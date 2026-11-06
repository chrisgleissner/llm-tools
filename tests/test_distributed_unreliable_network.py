"""Unreliable Wi-Fi between a real worker and the real coordinator (COORD-003, TEST-006).

``tests/fault_proxy.py`` sits between the Python worker (``llm_tools.bridge_worker.run`` in a
thread) and the release coordinator binary. The worker's coordinator URL points at the proxy,
TLS passes through it, and the tests reset connections, stall them without closing (half-open),
refuse reconnects, and add latency, jitter and throttling.

Timing. The coordinator's own ping interval and silence limit are fixed in the binary (20 s and
60 s), so they cannot be shortened. What a test waits for is therefore the worker's side: its
ping interval and silence limit are module constants of ``llm_tools.bridge_worker`` and are
patched small here (``PING_INTERVAL`` 0.3 s, ``SILENCE_LIMIT`` 1.5 s), and its reconnect backoff
is capped with ``max_backoff``. The backoff loop restarts at 1 s whenever the coordinator is
heard, so one wait is at most 1 s before the first retry and at most ``max_backoff`` after.
Nothing sleeps for a fixed time: every wait is a ``wait_for`` on an observable condition.

All randomness (jitter, flap timing) comes from seeded ``random.Random`` instances.
"""

from __future__ import annotations

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from llm_tools import bridge_worker as worker
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled, wait_for
from tests.distributed_harness import create_mission, requires_coordinator
from tests.fault_proxy import UP, FaultProxy
from tests.test_distributed_integration import Coordinator, _online

pytestmark = [requires_coordinator]

PING_INTERVAL = 0.3
SILENCE_LIMIT = 1.5
MAX_BACKOFF = 0.5
# A reset connection is noticed at once. The worker waits at most 1 s, then connects, and the
# handshake crosses the proxy with its injected delay; 5 s leaves headroom for a loaded host.
RECONNECT_BOUND = 5.0
# A half-open link is noticed only through silence: SILENCE_LIMIT plus one ping interval and one idle read.
STALL_DETECTION_BOUND = SILENCE_LIMIT + PING_INTERVAL + 1.0
MAX_OPEN_COMMANDS = 1000  # coordinator/src/db.rs MAX_OPEN_COMMANDS_PER_WORKER
OUTBOX_BOUND = 1024  # WorkerSession.outbox maxsize


@dataclass
class Link:
    coordinator: Coordinator
    proxy: FaultProxy
    session: worker.WorkerSession
    mission_id: str
    state: Path
    log: list[tuple[float, str]]

    def online(self) -> bool:
        return _online(self.coordinator)

    def offline(self) -> bool:
        status, body = self.coordinator.request("GET", "/api/v1/workers")
        return status == 200 and any(item["name"] == "ws" and item["state"] == "offline" for item in body["workers"])

    def reconnected_since(self, accepted: int) -> bool:
        return self.proxy.accepted > accepted and self.online()

    def submit(
        self,
        request: CommandRequest,
        *,
        wait_ms: int = 0,
        token: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[int, dict]:
        return self.coordinator.request(
            "POST", "/api/v1/commands", {"target": "ws", "request": request.to_json(), "wait_ms": wait_ms},
            token=token, source_ip=source_ip,
        )

    def command_state(self, request_id: str) -> str | None:
        status, body = self.coordinator.request("GET", f"/api/v1/commands/{request_id}")
        return body.get("state") if status == 200 else None

    def store(self) -> BridgeStore:
        return BridgeStore(self.state / "bridge.sqlite3")

    def worker_event_ids(self) -> list[int]:
        with self.store() as store:
            return [row[0] for row in store.db.execute("SELECT id FROM events ORDER BY id")]

    def coordinator_event_ids(self) -> list[int]:
        ids: list[int] = []
        while True:
            after = max(ids, default=0)
            status, body = self.coordinator.request("GET", f"/api/v1/events?worker=ws&after={after}&limit=1000")
            assert status == 200
            if not body["events"]:
                return ids
            ids.extend(item["id"] for item in body["events"])

    def add_events(self, count: int, tag: str) -> None:
        with self.store() as store:
            for index in range(count):
                store.record_runtime_event(self.mission_id, "link-probe", {"tag": tag, "n": index})

    def replicated(self) -> bool:
        with self.store() as store:
            last = store.last_event_id()
        status, body = self.coordinator.request("GET", "/api/v1/workers")
        return status == 200 and any(item["name"] == "ws" and item["event_cursor"] == last for item in body["workers"])

    def assert_replicated_exactly(self) -> None:
        wait_for(self.replicated, timeout=30, interval=0.1, message="worker events were not replicated after recovery")
        mine, theirs = self.worker_event_ids(), self.coordinator_event_ids()
        assert theirs == mine, f"lost {sorted(set(mine) - set(theirs))[:5]}, duplicated or invented {sorted(set(theirs) - set(mine))[:5]}"
        assert len(theirs) == len(set(theirs))

    def duplicate_deliveries(self) -> set[str]:
        entries = worker.read_audit(100_000)
        return {entry["request_id"] for entry in entries if entry.get("action") == "command" and entry.get("duplicate")}


@pytest.fixture
def link(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(worker, "PING_INTERVAL", PING_INTERVAL)
    monkeypatch.setattr(worker, "SILENCE_LIMIT", SILENCE_LIMIT)
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    mission_id = create_mission(tmp_path, capsys)
    coordinator = Coordinator(tmp_path / "coordinator")
    proxy = FaultProxy(coordinator.port, seed=20261105)
    token = coordinator.run("worker", "add", "ws")
    worker.enroll("ws", f"wss://localhost:{proxy.port}", token, ca_pem=(coordinator.data / "ca.pem").read_text(), allow_control=True)
    session = worker.default_session("test")
    log: list[tuple[float, str]] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=worker.run, args=(session, worker.websocket_connector()),
        kwargs={"stop": stop, "idle": 0.05, "max_backoff": MAX_BACKOFF, "log": lambda text: log.append((time.monotonic(), text))}, daemon=True,
    )
    thread.start()
    try:
        wait_for(lambda: _online(coordinator), timeout=15, interval=0.05, message="worker never came online through the proxy")
        yield Link(coordinator, proxy, session, mission_id, state, log)
    finally:
        stop.set()
        proxy.restore()
        thread.join(scaled(15))
        proxy.close()
        coordinator.stop()


def test_worker_reconnects_within_bound_after_repeated_resets(link):
    link.proxy.set_delay(0.02, 0.02)
    durations = []
    for _ in range(4):
        wait_for(lambda: link.proxy.active() >= 1, timeout=10, interval=0.02, message="no live connection to reset")
        accepted = link.proxy.accepted
        started = time.monotonic()
        assert link.proxy.reset_all() >= 1
        wait_for(lambda: link.reconnected_since(accepted), timeout=RECONNECT_BOUND, interval=0.02, message="worker did not reconnect after a reset")
        durations.append(time.monotonic() - started)
        wait_for(lambda: link.proxy.active() >= 1, timeout=10, interval=0.02)
    assert max(durations) < scaled(RECONNECT_BOUND), durations
    assert link.proxy.resets >= 4
    assert any("worker connection ended" in text for _, text in link.log)


def test_half_open_stall_is_detected_by_silence_and_recovered(link):
    accepted = link.proxy.accepted
    mark = len(link.log)
    started = time.monotonic()
    link.proxy.stall()
    wait_for(lambda: any("TimeoutError" in text for _, text in link.log[mark:]), timeout=STALL_DETECTION_BOUND, interval=0.02,
             message="worker did not give up on a silent link")
    detected = next(when for when, text in link.log[mark:] if "TimeoutError" in text) - started
    assert detected < scaled(STALL_DETECTION_BOUND), detected
    # The retry is stalled too (its TLS handshake never completes), while the coordinator still lists the old connection.
    wait_for(lambda: link.proxy.accepted > accepted, timeout=STALL_DETECTION_BOUND, interval=0.05, message="worker did not retry after giving up")
    link.proxy.reset_all()  # the access point finally drops the half-open connections
    link.proxy.restore()
    healed = time.monotonic()
    wait_for(lambda: link.online() and link.proxy.accepted > accepted, timeout=RECONNECT_BOUND, interval=0.02, message="worker did not recover after the stall")
    assert time.monotonic() - healed < scaled(RECONNECT_BOUND)


def test_cut_off_worker_is_offline_and_stale_and_its_events_replicate_after_recovery(link):
    link.add_events(50, "before")
    link.assert_replicated_exactly()
    link.proxy.cut()
    wait_for(link.offline, timeout=RECONNECT_BOUND, interval=0.05, message="coordinator still lists the cut-off worker as online")
    status, body = link.coordinator.request("GET", "/api/v1/missions")
    mine = [item for item in body["missions"] if item["worker"] == "ws"]
    assert status == 200 and mine and all(item["stale"] for item in mine), body

    refused = link.proxy.refused
    link.add_events(1500, "during")  # more than seven replication batches of 200, produced with no connection
    wait_for(lambda: link.proxy.refused >= refused + 2, timeout=10, interval=0.05, message="worker stopped retrying while cut off")
    assert link.offline()
    assert link.session.outbox.qsize() <= OUTBOX_BOUND
    assert max(link.coordinator_event_ids(), default=0) < link.worker_event_ids()[-1]  # nothing leaked through the cut

    accepted = link.proxy.accepted
    link.proxy.restore()
    wait_for(lambda: link.reconnected_since(accepted), timeout=RECONNECT_BOUND, interval=0.05, message="worker did not come back after the link returned")
    link.assert_replicated_exactly()
    assert len(link.worker_event_ids()) >= 1550


def test_command_executed_but_unacknowledged_is_redelivered_and_applied_once(link, monkeypatch):
    monkeypatch.setattr(worker, "SILENCE_LIMIT", 30.0)  # this test holds one direction silent on purpose
    with link.store() as store:
        tasks_before = len(store.list_tasks(link.mission_id))
    link.proxy.stall(UP)  # worker to coordinator: results and acks are held, commands still arrive
    request = CommandRequest("task.add", mission_id=link.mission_id, target="ws", request_id="dup-1", params={"prompt": "added once"})
    status, _ = link.submit(request)
    assert status in {200, 202}
    with link.store() as probe:
        wait_for(lambda: probe.command_exists("dup-1"), timeout=10, interval=0.02, message="worker never executed the command")
    assert link.command_state("dup-1") in {"pending", "sent"}  # the coordinator has no result yet
    accepted = link.proxy.accepted
    link.proxy.reset_all()
    link.proxy.restore()
    wait_for(lambda: link.reconnected_since(accepted), timeout=RECONNECT_BOUND, interval=0.05, message="worker did not reconnect")
    wait_for(lambda: link.command_state("dup-1") == "done", timeout=10, interval=0.05, message="redelivered command did not complete")
    assert "dup-1" in link.duplicate_deliveries()  # the worker saw the same request id again and did not run it again
    with link.store() as store:
        assert len(store.list_tasks(link.mission_id)) == tasks_before + 1
        assert store.db.execute("SELECT COUNT(*) FROM commands WHERE id='dup-1'").fetchone()[0] == 1
    status, body = link.coordinator.request("GET", "/api/v1/commands/dup-1")
    assert body["result"]["outcome"] == "applied"


def test_commands_submitted_while_the_link_flaps_are_applied_exactly_once(link):
    link.proxy.set_delay(0.01, 0.03)
    stop = threading.Event()
    flap_rng = random.Random(7)

    def flap() -> None:
        while not stop.is_set():
            stop.wait(flap_rng.uniform(0.1, 0.4))
            if flap_rng.random() < 0.3:
                link.proxy.refuse()
                stop.wait(flap_rng.uniform(0.05, 0.2))
                link.proxy.allow()
            link.proxy.reset_all()

    flapper = threading.Thread(target=flap, daemon=True)
    count = 60
    ids = [f"flap-{index}" for index in range(count)]
    tokens = link.coordinator.mutation_tokens(2)
    with link.store() as store:
        tasks_before = len(store.list_tasks(link.mission_id))
    flapper.start()
    try:
        def submit(index: int) -> int:
            request = CommandRequest("task.add", mission_id=link.mission_id, target="ws", request_id=ids[index], params={"prompt": f"flap task {index}"})
            return link.submit(request, token=tokens[index % len(tokens)])[0]

        with ThreadPoolExecutor(6) as pool:
            statuses = list(pool.map(submit, range(count)))
        assert set(statuses) <= {200, 202}, statuses
        wait_for(lambda: link.proxy.resets >= 8, timeout=20, interval=0.1, message="the link did not flap enough")
    finally:
        stop.set()
        flapper.join(5)
        link.proxy.restore()
    wait_for(lambda: all(link.command_state(request_id) == "done" for request_id in ids), timeout=40, interval=0.2,
             message="not every command completed after the link recovered")
    with link.store() as store:
        stored = [row[0] for row in store.db.execute("SELECT id FROM commands WHERE id LIKE 'flap-%' ORDER BY id")]
        assert sorted(stored) == sorted(ids)
        prompts = [task["prompt"] for task in store.list_tasks(link.mission_id)][tasks_before:]
    assert sorted(prompts) == sorted(f"flap task {index}" for index in range(count)), "a command was applied twice or not at all"
    for request_id in ids:
        status, body = link.coordinator.request("GET", f"/api/v1/commands/{request_id}")
        assert body["result"]["outcome"] == "applied", (request_id, body)
    print(f"resets={link.proxy.resets} connections={link.proxy.accepted} duplicate deliveries absorbed={len(link.duplicate_deliveries())}")


def test_queues_stay_bounded_while_the_worker_is_cut_off(link):
    link.proxy.cut()
    wait_for(link.offline, timeout=RECONNECT_BOUND, interval=0.05)
    tokens = link.coordinator.mutation_tokens(34)

    def submit(index: int) -> int:
        request = CommandRequest("mission.list", target="ws", request_id=f"queue-{index}")
        return link.submit(request, token=tokens[index % len(tokens)], source_ip=f"127.0.0.{2 + index % 9}")[0]

    with ThreadPoolExecutor(16) as pool:
        statuses = list(pool.map(submit, range(MAX_OPEN_COMMANDS + 1)))
    accepted = [status for status in statuses if status in {200, 202}]
    rejected = [status for status in statuses if status == 429]
    assert len(accepted) == MAX_OPEN_COMMANDS and len(rejected) == 1, sorted(set(statuses))
    status, diagnostics = link.coordinator.request("GET", "/api/v1/diagnostics")
    depths = diagnostics["command_queue_depths"]["ws"]
    assert status == 200 and sum(depths.values()) <= MAX_OPEN_COMMANDS and depths.get("pending") == MAX_OPEN_COMMANDS, depths
    assert link.session.outbox.maxsize == OUTBOX_BOUND and link.session.outbox.qsize() <= OUTBOX_BOUND
    queue_full_token = link.coordinator.run("token", "create", "queue-over", "--scope", "admin")
    status, body = link.submit(
        CommandRequest("mission.list", target="ws", request_id="queue-over"),
        token=queue_full_token, source_ip="127.0.0.11",
    )
    assert status == 429 and body["error"]["code"] == "queue-full"

    link.proxy.restore()
    wait_for(link.online, timeout=RECONNECT_BOUND, interval=0.05, message="worker did not reconnect")

    def drained() -> bool:
        _, snapshot = link.coordinator.request("GET", "/api/v1/diagnostics")
        return not snapshot["command_queue_depths"].get("ws")

    wait_for(drained, timeout=60, interval=0.25, message="the command queue did not drain after recovery")
    queue_after_token = link.coordinator.run("token", "create", "queue-after", "--scope", "admin")
    status, _ = link.submit(
        CommandRequest("mission.list", target="ws", request_id="queue-after"), wait_ms=int(scaled(10) * 1000),
        token=queue_after_token, source_ip="127.0.0.11",
    )
    assert status == 200  # the queue accepts work again


def test_slow_jittery_throttled_link_loses_nothing(link):
    link.proxy.set_delay(0.03, 0.03)
    link.proxy.set_throttle(up_bytes_per_second=64 * 1024, down_bytes_per_second=64 * 1024)
    link.add_events(500, "slow")
    request = CommandRequest("task.add", mission_id=link.mission_id, target="ws", request_id="slow-1", params={"prompt": "over a slow link"})
    status, body = link.submit(request, wait_ms=int(scaled(20) * 1000))
    assert status == 200 and body["state"] == "done", body
    link.assert_replicated_exactly()
    assert link.proxy.forwarded[UP] > 100_000  # the events really crossed the throttled link
    assert json.dumps(body["result"]["outcome"]) == '"applied"'
