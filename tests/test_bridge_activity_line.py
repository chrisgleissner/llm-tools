"""PRD-037 WP-7: the bounded `mission.activity_line` read."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from llm_tools import bridge_commands as commands
from llm_tools import bridge_review
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_store import BridgeStore


@pytest.fixture
def state(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "no-config.toml"))
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    return directory, created["mission"]["id"], int(created["task"]["id"])


def _running_operation(directory, mission_id, task_id, log: Path | None) -> str:
    with BridgeStore(directory / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.mark_operation_running(operation, pane_id="%1", process_pid=4242, process_start="boot", route_id="codex", model="gpt")
        if log is not None:
            store.record_operation_log_path(operation, str(log))
        return operation


def _activity(mission_id):
    result = commands.execute(CommandRequest("mission.activity_line", mission_id=mission_id))
    assert result.outcome == "ok", result
    return result.result


def test_activity_line_is_advertised_as_a_read_operation():
    catalog = {entry["operation"]: entry for entry in commands.operation_catalog()}
    assert catalog["mission.activity_line"]["mutating"] is False and catalog["mission.activity_line"]["params"] == {}


def test_activity_line_returns_one_sanitized_bounded_line(state, tmp_path):
    directory, mission_id, task_id = state
    log = tmp_path / "attempt-1.out"
    padding = "".join(f"earlier line {index}\n" for index in range(5000))  # far more than the bytes that are read
    log.write_bytes((padding + "\x1b[1;32mRunning tests\x1b[0m \x07\u202e now\r\n\u2500\u2500\u2500\u2500 \u2502 \u2502\n   \n").encode())
    _running_operation(directory, mission_id, task_id, log)
    result = _activity(mission_id)
    assert result["available"] is True and result["reason"] is None
    assert result["line"] == "Running tests now"
    assert result["task"] == 1 and result["provider"] == "codex"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", result["at"])
    assert set(result) == {"schema_version", "available", "reason", "message", "line", "at", "task", "provider"}

    log.write_bytes(("x" * 5000 + "\n").encode())
    long = _activity(mission_id)["line"]
    assert len(long) == bridge_review.ACTIVITY_LINE_CHARS and long.endswith("\u2026")


def test_activity_line_reads_only_the_end_of_the_log(state, tmp_path):
    directory, mission_id, task_id = state
    log = tmp_path / "attempt-1.out"
    log.write_bytes(b"SECRET-EARLY-OUTPUT\n" + b"y" * (bridge_review.ACTIVITY_TAIL_BYTES * 2))
    _running_operation(directory, mission_id, task_id, log)
    line = _activity(mission_id)["line"]
    assert "SECRET" not in line and set(line.rstrip("\u2026")) == {"y"}


@pytest.mark.parametrize("prepare,reason", [
    ("idle", "not-running"), ("no-log", "no-log"), ("missing", "unreadable"), ("empty", "no-output"), ("fifo", "unreadable"),
])
def test_activity_line_says_why_there_is_none(state, tmp_path, prepare, reason):
    directory, mission_id, task_id = state
    log: Path | None = tmp_path / "attempt-1.out"
    assert log is not None
    if prepare == "empty":
        log.write_bytes(b"\x1b[2J\n\n\xe2\x94\x80\n")
    elif prepare == "fifo":
        os.mkfifo(log)
    elif prepare == "no-log":
        log = None
    if prepare != "idle":
        _running_operation(directory, mission_id, task_id, log)
    result = _activity(mission_id)
    assert result["available"] is False and result["reason"] == reason and result["line"] is None and result["at"] is None
    assert result["message"]


def test_activity_line_of_an_unknown_mission_is_not_found(state):
    result = commands.execute(CommandRequest("mission.activity_line", mission_id="f" * 32))
    assert (result.outcome, result.error.code) == ("rejected", "not-found")
