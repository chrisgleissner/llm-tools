"""Worker event replication: one batch in flight, stale nacks, bounded batches, rate cap, unreplicated plumbing.

The slow-link tests run the real ``worker.run`` loop against a simulated link on a virtual clock, so
they are deterministic and never sleep. The simulated coordinator answers exactly like the deployed
protocol v1 coordinator: it replies ``welcome`` to ``hello``, ``pong`` to ``ping``, ``events.nack``
to a batch whose ``after`` differs from its cursor, and nothing to an accepted batch.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_worker as worker
from llm_tools.bridge_protocol import Message, decode
from llm_tools.bridge_store import BridgeStore

REPLICATED_KINDS = ("task-added", "operation-prepared", "provider-wait", "task-verified")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SlowCoordinatorLink:
    """A worker connection over a bandwidth-limited, delayed link to a protocol v1 coordinator."""

    def __init__(self, clock: Clock, stop: threading.Event, *, bytes_per_second: float, latency: float,
                 rewind_to: int | None = None, rewind_once_past: int = 0) -> None:
        self.clock, self.stop = clock, stop
        self.bytes_per_second, self.latency = bytes_per_second, latency
        self.up: deque[tuple[float, str]] = deque()  # (arrival time at the coordinator, text)
        self.down: deque[tuple[float, str]] = deque()
        self.link_free = clock.now
        self.cursor = 0  # the coordinator's stored cursor
        self.rewind_to, self.rewind_once_past = rewind_to, rewind_once_past
        self.sent_bytes: dict[str, int] = {}
        self.coordinator_messages: set[str] = set()
        self.nacks = 0
        self.max_batches_unprocessed = 0
        self.last_event_batch_end = 0.0
        self.deadline = clock.now + 3600.0
        self.generation = 0
        self.closed = False

    def send_text(self, text: str) -> None:
        message = decode(text)
        self.sent_bytes[message.type] = self.sent_bytes.get(message.type, 0) + len(text.encode())
        self.link_free = max(self.link_free, self.clock.now) + len(text.encode()) / self.bytes_per_second
        self.up.append((self.link_free + self.latency, text))
        self.max_batches_unprocessed = max(self.max_batches_unprocessed,
                                           sum(1 for _, queued in self.up if decode(queued).type == "events"))

    def _reply(self, kind: str, arrival: float, payload: dict[str, Any]) -> None:
        self.coordinator_messages.add(kind)
        self.down.append((arrival + self.latency, Message(kind, "coordinator", self.generation, payload).encode()))

    def _process(self, arrival: float, text: str) -> None:
        message = decode(text)
        if message.type == "hello":
            self.generation += 1
            self._reply("welcome", arrival, {"generation": self.generation, "protocol_version": 1, "event_cursor": self.cursor})
        elif message.type == "ping":
            self._reply("pong", arrival, {})
        elif message.type == "events":
            if message.payload["after"] != self.cursor:
                self.nacks += 1
                self._reply("events.nack", arrival, {"expected_after": self.cursor})
                return
            self.cursor = message.payload["through"]
            if self.rewind_to is not None and self.cursor >= self.rewind_once_past:
                # The coordinator loses part of what it stored (for example a restore from a backup).
                self.cursor, self.rewind_to = self.rewind_to, None

    def receive_text(self, timeout: float | None = None) -> str | None:
        limit = self.clock.now + (timeout or 0.0)
        while True:
            next_up = self.up[0][0] if self.up else float("inf")
            next_down = self.down[0][0] if self.down else float("inf")
            moment = min(next_up, next_down)
            if moment > limit:
                self.clock.now = limit
                if self.clock.now > self.deadline:
                    self.stop.set()
                return None
            self.clock.now = max(self.clock.now, moment)
            if next_up <= next_down:
                arrival, text = self.up.popleft()
                self._process(arrival, text)
                continue
            return self.down.popleft()[1]

    def close(self) -> None:
        self.closed = True


def _config() -> worker.WorkerConfig:
    return worker.WorkerConfig("worker-id", "ws-1", "wss://coordinator.example", "t" * 40, False)


def _session(path: Path, clock: Clock) -> worker.WorkerSession:
    session = worker.WorkerSession(_config(), open_store=lambda: BridgeStore(path), software_version="9.9",
                                   save=lambda value: None, audit=lambda *args, **kwargs: None)
    session.clock = clock
    return session


def _seed(path: Path, *, events: int, payload_bytes: int = 450, heartbeats: int = 0, refreshes: int = 0,
          missions: int = 1) -> list[str]:
    ids: list[str] = []
    with BridgeStore(path) as store:
        for index in range(missions):
            folder = path.parent / f"m{index}"
            folder.mkdir(exist_ok=True)
            ids.append(store.create_mission(f"m{index}", "objective", cwd=folder)["id"])
        rows = []
        for index in range(events):
            mission = ids[index % missions]
            rows.append((mission, None, None, REPLICATED_KINDS[index % len(REPLICATED_KINDS)],
                         json.dumps({"n": index, "text": "x" * payload_bytes}), "2026-11-07T10:00:00+00:00"))
            if heartbeats and index % max(1, events // heartbeats) == 0:
                rows.append((mission, None, None, "controller-heartbeat", json.dumps({"controller_state": "RUNNING"}), "2026-11-07T10:00:00+00:00"))
            if refreshes and index % max(1, events // refreshes) == 0:
                rows.append((mission, None, None, "capacity-refresh", json.dumps({"n": index, "providers": {"codex": 0.5}}), "2026-11-07T10:00:00+00:00"))
        with store._transaction():
            store.db.executemany("INSERT INTO events(mission_id,task_id,operation_id,kind,data_json,created_at) VALUES(?,?,?,?,?,?)", rows)
    return ids


def _all_events(path: Path) -> list[dict[str, Any]]:
    with BridgeStore(path) as store:
        events: list[dict[str, Any]] = []
        while page := store.events_since(events[-1]["id"] if events else 0, limit=1000):
            events.extend(page)
        return events


def _run_over_link(path: Path, monkeypatch: pytest.MonkeyPatch, link_factory) -> tuple[SlowCoordinatorLink, Clock]:
    clock = Clock()
    stop = threading.Event()
    link = link_factory(clock, stop)
    monkeypatch.setattr(worker.time, "monotonic", clock)
    session = _session(path, clock)
    last_id = _all_events(path)[-1]["id"]
    original = link.receive_text

    def receive(timeout=None):
        text = original(timeout)
        if link.cursor >= last_id and not link.up:
            stop.set()
        return text

    link.receive_text = receive  # type: ignore[method-assign]
    worker.run(session, lambda config: link, stop=stop)
    return link, clock


def _history_bytes(path: Path) -> int:
    return sum(len(json.dumps(event, separators=(",", ":"))) for event in _all_events(path))


def test_slow_link_with_a_late_nack_sends_about_the_history_once(tmp_path, monkeypatch):
    """One coordinator cursor rewind (a restore) on a link slower than the worker can write.

    The previous worker had about six batches of 200 events in flight, so every one of them drew its
    own nack and a full resend from the rewound cursor.
    """
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=1200)
    needed = _history_bytes(path)
    events = _all_events(path)
    rewind_to = events[len(events) // 2 - 60]["id"]
    link, clock = _run_over_link(path, monkeypatch, lambda c, s: SlowCoordinatorLink(
        c, s, bytes_per_second=50_000, latency=0.3, rewind_to=rewind_to, rewind_once_past=events[len(events) // 2]["id"]))

    assert link.cursor == events[-1]["id"], "the whole history reached the coordinator"
    sent = link.sent_bytes["events"]
    assert sent <= 1.2 * needed, f"sent {sent} bytes of events for {needed} bytes of history ({link.nacks} nacks)"
    assert link.nacks == 1
    assert link.max_batches_unprocessed == 1
    assert link.coordinator_messages <= {"welcome", "pong", "events.nack"}  # nothing a v1 coordinator would not send


def test_slow_link_without_any_desync_sends_the_history_once(tmp_path, monkeypatch):
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=600)
    link, _ = _run_over_link(path, monkeypatch, lambda c, s: SlowCoordinatorLink(c, s, bytes_per_second=30_000, latency=0.5))
    assert link.cursor == _all_events(path)[-1]["id"] and link.nacks == 0
    assert link.sent_bytes["events"] <= 1.1 * _history_bytes(path)
    assert link.max_batches_unprocessed == 1


def test_a_coordinator_that_never_acknowledges_events_still_makes_progress(tmp_path, monkeypatch):
    """Deployed coordinators send nothing for an accepted batch; the pong that follows the batch confirms it."""
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=300)
    link, clock = _run_over_link(path, monkeypatch, lambda c, s: SlowCoordinatorLink(c, s, bytes_per_second=1_000_000, latency=0.01))
    assert link.cursor == _all_events(path)[-1]["id"]
    assert link.coordinator_messages <= {"welcome", "pong"}
    assert clock.now - 1000.0 < 120.0


def _welcomed(path: Path, clock: Clock, cursor: int = 0) -> worker.WorkerSession:
    session = _session(path, clock)
    session.handle(Message("welcome", "coordinator", 1, {"generation": 1, "protocol_version": 1, "event_cursor": cursor}))
    return session


def _confirm(session: worker.WorkerSession) -> None:
    session.barrier_ping()
    session.handle(Message("pong", "coordinator", 1, {}))


def test_only_one_batch_is_in_flight_until_the_pong_after_it(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=100)
    clock = Clock()
    session = _welcomed(path, clock)
    first = session.next_events()
    assert first is not None
    clock.now += 3600
    assert session.next_events() is None, "no second batch before the first is confirmed"
    session.handle(Message("pong", "coordinator", 1, {}))  # a keepalive pong nobody asked the batch about
    assert session.next_events() is None
    session.ping()  # keepalive sent before the barrier would answer first, in order
    session.barrier_ping()
    session.handle(Message("pong", "coordinator", 1, {}))
    assert session.inflight is not None, "the pong of an earlier keepalive does not confirm the batch"
    session.handle(Message("pong", "coordinator", 1, {}))
    assert session.inflight is None and session.cursor == first.payload["through"]
    second = session.next_events()
    assert second is not None and second.payload["after"] == first.payload["through"]


def test_stale_nacks_are_ignored_and_a_real_one_rewinds_once(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=200)
    clock = Clock()
    session = _welcomed(path, clock)
    nack = Message("events.nack", "coordinator", 1, {"expected_after": 0})

    session.handle(nack)  # nothing in flight
    assert session.cursor == 0 and session.inflight is None

    first = session.next_events()
    _confirm(session)
    clock.now += 60
    second = session.next_events()
    assert second.payload["after"] == first.payload["through"]
    session.handle(Message("events.nack", "coordinator", 1, {"expected_after": second.payload["after"]}))
    assert session.inflight is not None and session.inflight.after == second.payload["after"], "names the batch's own start: stale"

    session.handle(nack)  # the coordinator really is behind: rewind
    assert session.cursor == 0 and session.inflight is None
    clock.now += 60
    resent = session.next_events()
    assert resent.payload["after"] == 0 and resent.payload["through"] == first.payload["through"]

    session.handle(nack)  # a second nack for the superseded batch: ignored
    session.handle(nack)
    assert session.inflight is not None and session.inflight.after == 0
    assert session.cursor == 0
    _confirm(session)
    assert session.cursor == resent.payload["through"]


def test_a_nack_from_a_coordinator_that_is_ahead_skips_forward(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=100)
    clock = Clock()
    session = _welcomed(path, clock)
    batch = session.next_events()
    ahead = batch.payload["through"] + 10
    session.handle(Message("events.nack", "coordinator", 1, {"expected_after": ahead}))
    assert session.cursor == ahead and session.inflight is None


def _drain(path: Path, clock: Clock, *, cursor: int = 0, step: float = 0.5, limit: int = 10_000) -> tuple[list[Message], worker.WorkerSession]:
    session = _welcomed(path, clock, cursor)
    batches: list[Message] = []
    for _ in range(limit):
        batch = session.next_events()
        if batch is not None:
            batches.append(batch)
            _confirm(session)
        elif session.cursor == _all_events(path)[-1]["id"]:
            break
        clock.now += step
    return batches, session


def test_heartbeats_are_not_replicated_and_only_the_newest_refresh_of_each_mission_is(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    ids = _seed(path, events=200, heartbeats=150, refreshes=40, missions=2)
    with BridgeStore(path) as store:  # the history ends in plumbing, which must still advance the cursor
        store.db.execute("INSERT INTO events(mission_id,kind,data_json,created_at) VALUES(?,?,?,?)",
                         (ids[0], "controller-heartbeat", "{}", "2026-11-07T10:00:00+00:00"))
    stored = _all_events(path)
    batches, session = _drain(path, Clock())

    sent = [event for batch in batches for event in batch.payload["events"]]
    assert not [event for event in sent if event["kind"] == "controller-heartbeat"]
    newest = {mission: max(e["id"] for e in stored if e["kind"] == "capacity-refresh" and e["mission_id"] == mission) for mission in ids}
    assert {(e["mission_id"], e["id"]) for e in sent if e["kind"] == "capacity-refresh"} == set(newest.items())
    expected_other = [e["id"] for e in stored if e["kind"] not in {"controller-heartbeat", "capacity-refresh"}]
    assert [e["id"] for e in sent if e["kind"] not in {"capacity-refresh"}] == expected_other
    assert session.cursor == stored[-1]["id"]
    for batch in batches:  # every batch starts where the previous one ended, holes included
        decode(batch.encode())
    assert [b.payload["after"] for b in batches[1:]] == [b.payload["through"] for b in batches[:-1]]
    sent_bytes = sum(len(b.encode()) for b in batches)
    all_bytes = sum(len(json.dumps(e, separators=(",", ":"))) for e in stored)
    assert sent_bytes < all_bytes


def test_a_history_of_only_plumbing_advances_the_cursor_with_an_empty_batch(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    ids = _seed(path, events=0)
    with BridgeStore(path) as store:
        store.db.executemany("INSERT INTO events(mission_id,kind,data_json,created_at) VALUES(?,?,?,?)",
                             [(ids[0], "controller-heartbeat", "{}", "2026-11-07T10:00:00+00:00")] * 30)
    last = _all_events(path)[-1]["id"]
    batches, session = _drain(path, Clock(), cursor=_all_events(path)[0]["id"])  # the mission's own events are already stored
    assert session.cursor == last
    assert len(batches) == 1 and batches[0].payload["events"] == [] and batches[0].payload["through"] == last


def test_batches_are_bounded_by_count_and_bytes(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=120, payload_bytes=2000)  # about 2 KB each: the byte bound applies before the count bound
    batches, _ = _drain(path, Clock())
    assert len(batches) > 120 // worker.EVENT_BATCH
    for batch in batches:
        events = batch.payload["events"]
        assert len(events) <= worker.EVENT_BATCH
        assert len(events) == 1 or sum(len(json.dumps(e, separators=(",", ":"))) for e in events) <= worker.EVENT_BATCH_BYTES
    _seed(tmp_path / "small.sqlite3", events=100, payload_bytes=20)
    small, _ = _drain(tmp_path / "small.sqlite3", Clock())
    assert max(len(batch.payload["events"]) for batch in small) == worker.EVENT_BATCH


def test_an_event_larger_than_the_byte_bound_is_still_sent_alone(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=5, payload_bytes=worker.EVENT_BATCH_BYTES + 100)
    batches, session = _drain(path, Clock())
    assert [len(batch.payload["events"]) for batch in batches if batch.payload["events"]][-5:] == [1] * 5
    assert session.cursor == _all_events(path)[-1]["id"]


def test_the_rate_cap_holds_back_batches(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=400)
    clock = Clock()
    start = clock.now
    session = _welcomed(path, clock)
    total, largest = 0, 0
    while session.cursor != _all_events(path)[-1]["id"]:
        batch = session.next_events()
        if batch is not None:
            size = len(batch.encode())
            total, largest = total + size, max(largest, size)
            _confirm(session)
            assert clock.now - start <= 600
        clock.now += 0.05
    elapsed = clock.now - start
    # The first batch is free; after that the sustained rate stays at or below the cap.
    assert total - largest <= worker.EVENT_RATE_BYTES_PER_SECOND * elapsed
    assert elapsed > (total - largest) / (worker.EVENT_RATE_BYTES_PER_SECOND * 1.05)


def test_welcome_resets_replication_state_for_the_new_connection(tmp_path):
    path = tmp_path / "bridge.sqlite3"
    _seed(path, events=100)
    clock = Clock()
    session = _welcomed(path, clock)
    assert session.next_events() is not None and session.inflight is not None
    session.handle(Message("welcome", "coordinator", 2, {"generation": 2, "protocol_version": 1, "event_cursor": 0}))
    assert session.inflight is None and session.send_not_before == 0.0 and not session.pending_pongs
    assert session.next_events() is not None
