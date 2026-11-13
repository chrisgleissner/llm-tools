"""The worker's hello stays inside the coordinator's frame limit; the rest of the missions follow in events batches."""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

from llm_tools import bridge_worker as worker
from llm_tools.bridge_protocol import decode

from .test_bridge_worker import _session, state  # noqa: F401 - the fixture

FRAME_LIMIT = 1024 * 1024  # coordinator MAX_MESSAGE_BYTES


def _summary(index: int, status: str) -> dict:
    return {"id": f"mission-{index:05d}", "name": f"mission {index} parser cleanup", "status": status, "folder": "project-dir",
            "updated_at": f"2026-01-01T{index:08d}", "task_counts": {"COMPLETED": 36, "QUEUED": 4},
            "current_task": {"ordinal": 37, "title": "Add the retry path to the scheduler and cover it with a test"},
            "station": "claude", "model": "claude-sonnet-5", "needs_kind": None, "needs_tasks": 0}


def _wire_session(state, monkeypatch, active: int, finished: int):
    directory, _ = state
    session, _ = _session(directory)
    rows = [{"id": f"mission-{i:05d}", "status": "RUNNING" if i < active else "COMPLETED"} for i in range(active + finished)]
    summaries = {row["id"]: _summary(int(row["id"].split("-")[1]), row["status"]) for row in rows}
    store = SimpleNamespace(list_missions=lambda: rows, last_event_id=lambda: 7, pending_tombstones=lambda limit: [], events_since=lambda after, limit=0: [], get_mission=lambda mission_id: None)

    @contextlib.contextmanager
    def open_store():
        yield store

    monkeypatch.setattr(session, "open_store", open_store)
    monkeypatch.setattr(session, "_mission_summary", lambda _store, row: dict(summaries[row["id"]]))
    return session, set(summaries)


def test_hello_with_4500_missions_is_bounded_and_the_rest_follow_in_events(state, monkeypatch):
    session, every_id = _wire_session(state, monkeypatch, active=300, finished=4200)
    hello = session.hello()
    frame = hello.encode()
    assert len(frame.encode()) <= worker.HELLO_FRAME_BUDGET < FRAME_LIMIT
    decoded = decode(frame)  # the worker-side parser accepts it; the coordinator's validator has the same fields
    sent = decoded.payload["missions"]
    assert len(sent) <= worker.HELLO_MAX_MISSIONS
    ids = [item["id"] for item in sent]
    assert {f"mission-{i:05d}" for i in range(300)} <= set(ids), "every active mission is in the hello"
    finished_sent = sorted(i for i in ids if int(i.split("-")[1]) >= 300)
    assert finished_sent[-1] == "mission-04499", "the newest finished missions are chosen"

    session.cursor = 7
    seen = set(ids)
    batches = 0
    while (batch := session.next_events()) is not None:
        assert len(batch.encode().encode()) <= FRAME_LIMIT
        payload = decode(batch.encode()).payload
        assert payload["events"] == [] and payload["after"] == payload["through"] == 7
        seen.update(item["id"] for item in payload["missions"])
        session.inflight, session.send_not_before = None, 0.0  # the coordinator's pong confirmed the batch
        batches += 1
    assert batches >= 1 and seen == every_id


def test_hello_with_all_active_missions_over_the_count_defers_the_excess(state, monkeypatch):
    session, every_id = _wire_session(state, monkeypatch, active=2600, finished=0)
    hello = session.hello()
    assert len(hello.payload["missions"]) <= worker.HELLO_MAX_MISSIONS
    assert len(hello.payload["missions"]) + len(session.deferred_missions) == 2600
    assert len(hello.encode().encode()) <= worker.HELLO_FRAME_BUDGET


def test_a_small_hello_defers_nothing(state, monkeypatch):
    session, _ = _wire_session(state, monkeypatch, active=3, finished=5)
    assert len(session.hello().payload["missions"]) == 8 and session.deferred_missions == []


def test_a_refusal_notice_is_recorded_and_shown_by_worker_status(state):
    from llm_tools.bridge_cli_worker import _format_worker_status
    from llm_tools.bridge_protocol import Message

    directory, _ = state
    session, _ = _session(directory)
    notice = Message("notice", "c", 1, {"code": "missions-over-cap", "refused": 3, "cap": 10000, "message": "refused"})
    assert decode(notice.encode()).type == "notice"
    assert session.handle(notice) == []
    status = worker.status_public(service_state="active")
    assert status["missions_refused"] == 3
    assert "3 missions refused by coordinator cap" in _format_worker_status(status)
    session.handle(Message("notice", "c", 1, {"code": "missions-over-cap", "refused": 0, "cap": 10000, "message": "cleared"}))
    assert "missions_refused" not in worker.status_public(service_state="active")
