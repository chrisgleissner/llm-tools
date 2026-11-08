"""The worker's run loop must not keep objects per command or per event (PRD-007 soak, worker memory).

The soak showed worker processes growing by 1.9 to 2.5 MiB per hour after the first 20 minutes. This
module checks the part that can be checked without wall-clock time or RSS: after a warm-up, a fixed
number of simulated commands and event polls must not leave new interned strings (CPython 3.12 makes
every string passed to ``sys.intern`` immortal, and ``pathlib`` interns each path component it parses,
the pattern that leaked in the controller) and must not leave live Python objects behind.
"""

from __future__ import annotations

import gc
import sys

import pytest

from llm_tools import bridge_worker as worker
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_protocol import Message
from llm_tools.bridge_store import BridgeStore

needs_interned_count = pytest.mark.skipif(
    not hasattr(sys, "getunicodeinternedsize") or sys.version_info[:2] != (3, 12),
    reason="interned strings are immortal, and countable, only on CPython 3.12",
)

WARM_UP = 60
ROUNDS = 240


def _session(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setenv("HOME", str(tmp_path))
    mission_ids = []
    with BridgeStore(state / "bridge.sqlite3") as store:
        for index in range(3):
            folder = tmp_path / f"project-{index}"
            folder.mkdir()
            mission_ids.append(store.create_mission(f"m{index}", "objective", cwd=folder, initial_prompt="first")["id"])
    config = worker.enroll("w-memory", "wss://coordinator.invalid:8443", "t" * 40, ca_pem=None, allow_control=True)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(state / "bridge.sqlite3", readonly=True), software_version="test",
                                   save=lambda value: None, audit=lambda *args, **kwargs: None)
    session.handle(Message("welcome", "coordinator", 1, {"generation": 1, "protocol_version": 1, "event_cursor": 0}))
    session.cursor = 0
    return state, mission_ids, session


def _round(index: int, state, mission_ids, session) -> None:
    mission_id = mission_ids[index % len(mission_ids)]
    with BridgeStore(state / "bridge.sqlite3") as writer:
        with writer._transaction():
            writer._event(mission_id, "capacity-refresh", {"round": index})
    operation = ("mission.status", "task.list", "mission.list", "task.add")[index % 4]
    params = {"prompt": f"marker {index} {id(session)}"} if operation == "task.add" else {}
    request = CommandRequest(operation, params, None if operation == "mission.list" else mission_id, "w-memory", f"{index:032x}")
    replies = session.handle(Message("command", "coordinator", 1, request.to_json()))
    assert [reply.type for reply in replies] == ["command.ack", "command.result"]
    assert replies[1].payload["outcome"] in {"ok", "applied"}, replies[1].payload
    while session.next_events() is not None:
        pass


def _live_objects() -> int:
    gc.collect()
    return len(gc.get_objects())


@needs_interned_count
def test_worker_keeps_no_interned_strings_per_command(tmp_path, monkeypatch) -> None:
    state, mission_ids, session = _session(tmp_path, monkeypatch)
    for index in range(WARM_UP):
        _round(index, state, mission_ids, session)
    before = sys.getunicodeinternedsize()
    for index in range(WARM_UP, WARM_UP + ROUNDS):
        _round(index, state, mission_ids, session)
    assert sys.getunicodeinternedsize() - before <= 4, "the worker keeps a new interned string per command or event"


def test_worker_keeps_no_python_objects_per_command(tmp_path, monkeypatch) -> None:
    state, mission_ids, session = _session(tmp_path, monkeypatch)
    for index in range(WARM_UP):
        _round(index, state, mission_ids, session)
    before = _live_objects()
    for index in range(WARM_UP, WARM_UP + ROUNDS):
        _round(index, state, mission_ids, session)
    grown = _live_objects() - before
    assert grown <= ROUNDS // 4, f"{grown} objects are left after {ROUNDS} commands and event polls"
    assert session.outbox.qsize() == 0
    assert len(session.mission_questions) <= 3
