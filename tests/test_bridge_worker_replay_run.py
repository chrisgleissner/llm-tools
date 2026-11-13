"""``worker.run`` against a scripted coordinator: how a rejected replay batch is handled."""
from __future__ import annotations

import threading

from llm_tools import bridge_worker as worker
from llm_tools.bridge_protocol import Message, decode

from .test_bridge_worker import state  # noqa: F401 - the fixture
from .test_bridge_worker_hello import _wire_session


class Scripted:
    """Welcomes the worker, then answers each events batch with ``reply(batch)`` (an error code or None) and each ping with a pong."""

    def __init__(self, stop, reply, limit=40):
        self.stop, self.reply, self.limit = stop, reply, limit
        self.incoming = [Message("welcome", "c", 1, {"generation": 1, "protocol_version": 1, "event_cursor": 7}).encode()]
        self.sent = []

    def send_text(self, text):
        message = decode(text)
        self.sent.append(message)
        if message.type == "events":
            if sum(1 for m in self.sent if m.type == "events") > 8:
                self.stop.set()  # a loop of resends ends here, so the assertions below can name it
            code = self.reply(message)
            if code:
                self.incoming.append(Message("error", "c", 1, {"code": code, "message": "x", "in_reply_to": message.id}).encode())
        if message.type == "ping":
            self.incoming.append(Message("pong", "c", 1, {}).encode())

    def receive_text(self, timeout=None):
        if self.incoming:
            return self.incoming.pop(0)
        self.limit -= 1
        if self.limit <= 0:
            self.stop.set()
        return None

    def close(self):
        pass


def _replay_batches(state, monkeypatch, reply):
    monkeypatch.setattr(worker, "EVENT_RATE_BYTES_PER_SECOND", 10**12)
    session, _ = _wire_session(state, monkeypatch, active=0, finished=2600)
    stop = threading.Event()
    link = Scripted(stop, reply)
    worker.run(session, lambda config: link, stop=stop, idle=0.0)
    return [m for m in link.sent if m.type == "events" and m.payload.get("missions") and not m.payload.get("events")]


def test_a_persistent_error_for_a_replay_batch_is_not_resent(state, monkeypatch):
    # 2600 missions: 2000 in the hello and 600 in one deferred batch, which is sent once and then dropped.
    batches = _replay_batches(state, monkeypatch, lambda batch: "rate-limited")
    assert len(batches) == 1 and batches[0].payload["replay"] is True


def test_an_old_coordinator_gets_the_batch_once_more_without_the_marker(state, monkeypatch):
    batches = _replay_batches(state, monkeypatch, lambda batch: "invalid-message" if "replay" in batch.payload else None)
    assert [("replay" in b.payload) for b in batches] == [True, False]
    assert [m["id"] for m in batches[0].payload["missions"]] == [m["id"] for m in batches[1].payload["missions"]]


def test_the_unmarked_resend_is_not_repeated_when_it_is_rejected_too(state, monkeypatch):
    batches = _replay_batches(state, monkeypatch, lambda batch: "invalid-message")
    assert [("replay" in b.payload) for b in batches] == [True, False]
