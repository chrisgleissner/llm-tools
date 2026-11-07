"""PRD-037 WP-7: attention and task counts in the worker's mission summary, and heartbeat handling."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from llm_tools import bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_protocol import Message, decode
from llm_tools.bridge_store import BridgeStore

TOKEN = "t" * 40
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def state(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    return directory, created["mission"]["id"], int(created["task"]["id"])


def _session(directory):
    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=True)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(directory / "bridge.sqlite3"), software_version="9.9")
    return session


def _welcome(session, cursor):
    session.handle(Message("welcome", "coordinator", 1, {"generation": 1, "protocol_version": 1, "event_cursor": cursor}))


def _finish_first_task(directory, mission_id, task_id):
    with BridgeStore(directory / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)


def test_summary_carries_task_counts_and_the_attention_of_an_awaiting_review(state):
    directory, mission_id, task_id = state
    session = _session(directory)
    hello = session.hello()
    quiet = hello.payload["missions"][0]
    assert quiet["task_counts"] == {"QUEUED": 1} or quiet["task_counts"] == {"READY": 1}
    assert quiet["attention"] is None
    _welcome(session, hello.payload["event_cursor"])

    _finish_first_task(directory, mission_id, task_id)
    batch = session.next_events()
    assert batch is not None
    summary = {m["id"]: m for m in batch.payload["missions"]}[mission_id]
    assert summary["task_counts"] == {"VERIFYING": 1}
    assert set(summary["attention"]) == {"kind", "message", "tasks"}
    assert summary["attention"]["kind"] == "approval" and summary["attention"]["tasks"] == [1]
    assert summary["attention"]["message"].startswith("task 1 awaits your review")
    assert len(summary["attention"]["message"]) <= worker.ATTENTION_MESSAGE_CHARS
    decode(batch.encode())  # a valid protocol message


def test_attention_sees_an_awaiting_review_that_is_not_the_first_task(state):
    directory, mission_id, _ = state
    with BridgeStore(directory / "bridge.sqlite3") as store:
        second = store.add_task(mission_id, "Second task", title="Second")
        store.db.execute("UPDATE tasks SET status='VERIFYING' WHERE id=?", (second,))
        store.set_mission_status(mission_id, "RUNNING")
    summary = _session(directory).hello().payload["missions"][0]
    assert summary["attention"] is not None and summary["attention"]["kind"] == "approval"
    assert summary["attention"]["tasks"] == [2]


def test_summary_of_a_finished_mission_says_nothing_is_needed(state, capsys):
    directory, mission_id, _ = state
    assert main(["cancel", "--mission", mission_id, "--yes"]) == 0
    capsys.readouterr()
    summary = _session(directory).hello().payload["missions"][0]
    assert summary["status"] == "CANCELLED" and summary["attention"] is None


def test_attention_is_left_out_when_the_projection_cannot_be_read(state, monkeypatch):
    directory, _, _ = state

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("llm_tools.bridge_monitor.project_snapshot", broken)
    summary = _session(directory).hello().payload["missions"][0]
    assert "attention" not in summary and "task_counts" in summary  # absent means unknown, not "nothing needed"


def test_attention_is_bounded_and_free_of_control_characters():
    needs = {"kind": "provider", "command": "x" * 900, "tasks": [3, True, "2", 0, *range(10, 30)],
             "message": "Codex asks:\x1b[31m red\x07 " + "word " * 400 + "\u202e"}
    attention = worker.WorkerSession._bounded_attention(needs)
    assert attention is not None and set(attention) == {"kind", "message", "tasks"}
    assert len(attention["message"]) == worker.ATTENTION_MESSAGE_CHARS and attention["message"].endswith("\u2026")
    assert not any(ord(char) < 32 or char in "\u202e\x7f" for char in attention["message"])
    assert attention["tasks"] == [3, *range(10, 17)]
    assert worker.WorkerSession._bounded_attention({"kind": "surprise", "message": "x"}) is None
    assert worker.WorkerSession._bounded_attention(None) is None


def test_a_heartbeat_only_batch_sends_no_summary_but_still_advances_the_cursor(state):
    directory, mission_id, _ = state
    session = _session(directory)
    hello = session.hello()
    _welcome(session, hello.payload["event_cursor"])
    with BridgeStore(directory / "bridge.sqlite3") as store:
        store.record_runtime_event(mission_id, "controller-heartbeat", {"state": "RUNNING"})
        store.record_runtime_event(mission_id, "capacity-refresh", {})
    batch = session.next_events()
    assert batch is not None
    assert [event["kind"] for event in batch.payload["events"]] == ["controller-heartbeat", "capacity-refresh"]
    assert batch.payload["missions"] == []
    assert batch.payload["mission_cursor"] == batch.payload["through"]

    with BridgeStore(directory / "bridge.sqlite3") as store:
        store.record_runtime_event(mission_id, "controller-heartbeat", {"state": "RUNNING"})
        store.record_runtime_event(mission_id, "task-status", {"task_id": 1})
    mixed = session.next_events()
    assert mixed is not None and [m["id"] for m in mixed.payload["missions"]] == [mission_id]


def test_heartbeat_kinds_match_the_coordinators_list():
    source = (ROOT / "coordinator/src/protocol.rs").read_text()
    declared = re.search(r"HEARTBEAT_KINDS: \[&str; \d+\] = \[([^\]]*)\]", source)
    assert declared is not None
    assert set(re.findall(r'"([a-z-]+)"', declared.group(1))) == set(worker.HEARTBEAT_KINDS)
