"""The real worker hello and replay builder against the real coordinator, above the per-worker cap."""
from __future__ import annotations

import sqlite3
import ssl
import sys
from pathlib import Path

from llm_tools import bridge_ws
from llm_tools.bridge_protocol import decode

from .distributed_harness import requires_coordinator
from .test_bridge_worker import _session, state  # noqa: F401 - the fixture
from .test_bridge_worker_hello import _wire_session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dist_harness as h  # noqa: E402

CAP = 10_000
ACTIVE = 50
FINISHED = 12_000


def _exchange(client, session, until_type: str) -> None:
    """Read frames, feed them to the session and stop after one of ``until_type``."""
    while True:
        text = client.receive_text(timeout=30)
        assert text is not None, "the coordinator did not answer"
        message = decode(text)
        assert message.type != "error", message.payload
        session.handle(message)
        if message.type == until_type:
            return


def _connect(coordinator, session, token: str):
    context = ssl.create_default_context(cafile=str(coordinator.ca_file))
    client = bridge_ws.WebSocketClient.connect(f"wss://{h.HOST}:{coordinator.port}/api/v1/worker/connect",
                                               headers={"Authorization": f"Bearer {token}"}, context=context, timeout=30)
    session.generation, session.cursor, session.inflight = 0, None, None
    client.send_text(session.hello().encode())
    _exchange(client, session, "welcome")
    return client


def _replay(client, session, chunks: int | None = None) -> None:
    """Send the deferred batches the way ``run`` does (a batch, then a barrier ping), at most ``chunks`` of them."""
    sent = 0
    while chunks is None or sent < chunks:
        session.send_not_before = 0.0
        batch = session.next_events()
        if batch is None:
            return
        client.send_text(batch.encode())
        client.send_text(session.barrier_ping().encode())
        _exchange(client, session, "pong")
        sent += 1


def _stored(coordinator) -> dict[str, str]:
    connection = sqlite3.connect(f"file:{coordinator.db_path}?mode=ro", uri=True)
    try:
        return {mission_id: status for mission_id, status in connection.execute("SELECT mission_id, json_extract(info,'$.status') FROM missions")}
    finally:
        connection.close()


def _expected() -> set[str]:
    """Every active mission and the newest finished ones, which is what fits under the cap."""
    active = {f"mission-{i:05d}" for i in range(ACTIVE)}
    newest = {f"mission-{i:05d}" for i in range(ACTIVE + FINISHED - (CAP - ACTIVE), ACTIVE + FINISHED)}
    return active | newest


def _system(state, monkeypatch, tmp_path):
    session, every_id = _wire_session(state, monkeypatch, active=ACTIVE, finished=FINISHED)
    coordinator = h.Coordinator(h.find_coordinator_binary(), h.short_temp_dir("replay-") / "data")
    coordinator.setup()
    token = coordinator.add_worker(session.config.name)
    return session, every_id, coordinator, token


@requires_coordinator
def test_12000_finished_missions_over_the_cap_keep_the_active_and_the_newest_finished(state, monkeypatch, tmp_path):
    session, _, coordinator, token = _system(state, monkeypatch, tmp_path)
    try:
        client = _connect(coordinator, session, token)
        _replay(client, session)
        client.close()
        stored = _stored(coordinator)
        assert len(stored) == CAP
        assert set(stored) == _expected()
        assert sum(1 for status in stored.values() if status == "RUNNING") == ACTIVE
    finally:
        coordinator.stop()


@requires_coordinator
def test_a_connection_dropped_in_the_middle_of_the_replay_ends_with_the_same_set(state, monkeypatch, tmp_path):
    session, _, coordinator, token = _system(state, monkeypatch, tmp_path)
    try:
        client = _connect(coordinator, session, token)
        _replay(client, session, chunks=2)
        client.sock.close()  # no close frame: the connection is simply gone
        partial = _stored(coordinator)
        assert len(partial) < CAP + 1 and ACTIVE <= len(partial)
        client = _connect(coordinator, session, token)  # a new hello, and the replay starts over
        _replay(client, session)
        client.close()
        assert set(_stored(coordinator)) == _expected()
    finally:
        coordinator.stop()
