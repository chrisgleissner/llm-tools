"""Fault injection and concurrency for remote commands (TEST-006, TEST-007).

Real separate processes race on one worker database; storage failures and
lost replies are injected around the command layer and the worker session.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from llm_tools import bridge_commands as commands
from llm_tools import bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_protocol import Message, decode
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled

ROOT = Path(__file__).resolve().parents[1]
RACER = """
import json, sys
from llm_tools import bridge_commands as commands
request = commands.CommandRequest.from_json(json.loads(sys.argv[1]))
sys.stdin.readline()  # start together
print(json.dumps(commands.execute(request).to_json()))
"""


@pytest.fixture
def mission(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        task = store.list_tasks(mission_id)[0]
    return state, mission_id, task


def _race(requests: list[CommandRequest]) -> list[dict]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [subprocess.Popen([sys.executable, "-c", RACER, json.dumps(request.to_json())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT) for request in requests]
    try:
        for proc in procs:
            assert proc.stdin is not None
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=scaled(60))
            assert proc.returncode == 0, err
            results.append(json.loads(out))
        return results
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _rows(state: Path, sql: str, *args) -> list[tuple]:
    with BridgeStore(state / "bridge.sqlite3") as store:
        return [tuple(row) for row in store.db.execute(sql, args)]


def test_concurrent_redelivery_from_separate_processes_applies_once(mission):
    state, mission_id, task = mission
    request = CommandRequest("task.amend", {"task_id": task["id"], "prompt": "once"}, mission_id=mission_id, request_id="same-request")
    results = _race([request] * 6)
    assert {result["outcome"] for result in results} == {"applied"}
    assert len({json.dumps(result["result"], sort_keys=True) for result in results}) == 1
    assert _rows(state, "SELECT COUNT(*) FROM commands WHERE id='same-request'") == [(1,)]
    assert _rows(state, "SELECT revision - ?, prompt FROM tasks WHERE id=?", task["revision"], task["id"]) == [(1, "once")]


def test_conflicting_approvals_from_separate_processes_only_one_wins(mission):
    state, mission_id, task = mission
    requests = [CommandRequest("task.amend", {"task_id": task["id"], "prompt": f"version {index}", "expected_revision": task["revision"]},
                               mission_id=mission_id, request_id=f"amend-{index}") for index in range(6)]
    results = _race(requests)
    outcomes = sorted(result["outcome"] for result in results)
    assert outcomes == ["applied"] + ["rejected"] * 5
    assert {result["error"]["code"] for result in results if result["outcome"] == "rejected"} == {"conflict"}
    winner = next(index for index, result in enumerate(results) if result["outcome"] == "applied")
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(task["id"])["prompt"] == f"version {winner}"


def test_storage_failure_during_a_mutation_is_uncertain_rolls_back_and_redelivery_applies_once(mission, monkeypatch):
    state, mission_id, _ = mission
    real = BridgeStore.finish_command
    failures = iter([sqlite3.OperationalError("disk I/O error")])

    def flaky(self, *args, **kwargs):
        error = next(failures, None)
        if error is not None:
            raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(BridgeStore, "finish_command", flaky)
    request = CommandRequest("mission.cancel", mission_id=mission_id, request_id="cancel-1")
    first = commands.execute(request)
    assert first.outcome == "uncertain"
    assert _rows(state, "SELECT status FROM missions WHERE id=?", mission_id) == [("CREATED",)]  # effect rolled back with the failure
    again = commands.execute(request)
    assert again.outcome == "applied"
    assert commands.execute(request).result == again.result
    assert _rows(state, "SELECT status FROM missions WHERE id=?", mission_id) == [("CANCELLED",)]
    assert _rows(state, "SELECT COUNT(*) FROM commands WHERE id='cancel-1'") == [(1,)]


def test_locked_database_read_fails_cleanly(mission, monkeypatch):
    _, mission_id, _ = mission

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(BridgeStore, "list_tasks", locked)
    result = commands.execute(CommandRequest("task.list", mission_id=mission_id))
    assert result.outcome == "failed" and result.error.code == "internal" and "locked" not in json.dumps(result.to_json())


class Link:
    """One coordinator connection; ``drop_after_ack`` loses the link before the result is sent."""

    def __init__(self, inbox: list[str], drop_after_ack: bool = False) -> None:
        self.inbox = inbox
        self.sent: list[Message] = []
        self.drop_after_ack = drop_after_ack

    def send_text(self, text: str) -> None:
        message = decode(text)
        if self.drop_after_ack and message.type == "command.result":
            raise ConnectionResetError("partition")
        self.sent.append(message)

    def receive_text(self, timeout=None):
        if self.inbox:
            return self.inbox.pop(0)
        raise ConnectionResetError("closed")

    def close(self) -> None:
        pass


def test_partition_after_ack_redelivers_without_repeating_the_effect(mission):
    state, mission_id, _ = mission
    config = worker.enroll("ws", "wss://c:1", "t" * 40, ca_pem=None, allow_control=True)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(state / "bridge.sqlite3"), software_version="t", save=lambda value: None)
    command = CommandRequest("mission.cancel", mission_id=mission_id, request_id="cancel-across-partition").to_json()

    def connection(generation: int, drop: bool) -> Link:
        return Link([Message("welcome", "c", generation, {"generation": generation, "protocol_version": 1, "event_cursor": 0}).encode(),
                     Message("command", config.worker_id, generation, command).encode()], drop_after_ack=drop)

    links = iter([connection(1, True), connection(2, False)])
    stop = threading.Event()
    seen: list[Link] = []

    def connect(cfg):
        link = next(links, None)
        if link is None:
            stop.set()
            raise ConnectionRefusedError("done")
        seen.append(link)
        return link

    worker.run(session, connect, stop=stop, idle=0.01, max_backoff=0.01)
    first, second = seen
    assert [m.payload["status"] for m in first.sent if m.type == "command.ack"] == ["received"]
    assert not [m for m in first.sent if m.type == "command.result"]
    assert [m.payload["status"] for m in second.sent if m.type == "command.ack"] == ["duplicate"]
    result = next(m for m in second.sent if m.type == "command.result")
    assert result.payload["outcome"] == "applied"
    assert _rows(state, "SELECT status FROM missions WHERE id=?", mission_id) == [("CANCELLED",)]
    assert _rows(state, "SELECT COUNT(*) FROM events WHERE mission_id=? AND kind LIKE '%cancel%'", mission_id)[0][0] <= 1
