"""Worker link and browser stream byte budgets (DOG-020, DOG-021).

A real coordinator and a real worker thread run with 3 running and 31 finished missions whose tasks
carry 5 KB prompts. The worker connector counts the bytes of every message by type, the coordinator's
diagnostics count the same for the browser stream, and the tests compare them with the budgets:

* steady state with 3 running missions: under 2 KB/s on the worker link, whatever a console asks;
* one ``mission.status`` result: a few KB, so the result of a console's status poll stays small;
* after a reconnect: under 200 KB, with the queued writes replayed once and no read replayed;
* a read for an offline worker is refused at once (503 ``worker-offline``), not queued;
* the browser stream carries a change notice per accepted batch (counts, no event bodies).

Measured on the development host (release build), see docs/distributed/benchmarks.md.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import pytest

from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_store import BridgeStore
from llm_tools.bridge_ws import WebSocketClient
from tests.conftest import scaled, wait_for
from tests.distributed_harness import requires_coordinator, running_system

pytestmark = [requires_coordinator, pytest.mark.skipif(sys.platform == "win32", reason="workers are not supported on Windows")]

RUNNING, FINISHED = 3, 31
PROMPT = "Implement the requested change with all of the details given. " * 85  # about 5 KB

STEADY_BYTES_PER_SECOND = 2 * 1024  # worker link, both directions, 3 running missions
STATUS_RESULT_BYTES = 6 * 1024  # one mission.status result
RECONNECT_BYTES = 200 * 1024  # everything exchanged in the first seconds after a reconnect
STREAM_BYTES_PER_SECOND = 1024  # browser stream with the same workload
STREAM_MESSAGE_BYTES = 2 * 1024  # largest single notice


class LinkBytes:
    """Bytes and messages of the worker connection by direction and message type."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bytes: Counter[tuple[str, str]] = Counter()
        self.messages: Counter[tuple[str, str]] = Counter()

    def add(self, direction: str, text: str) -> None:
        kind = json.loads(text).get("type", "?")
        with self.lock:
            self.bytes[(direction, kind)] += len(text)
            self.messages[(direction, kind)] += 1

    def snapshot(self) -> tuple[Counter[tuple[str, str]], Counter[tuple[str, str]]]:
        with self.lock:
            return Counter(self.bytes), Counter(self.messages)

    def connector(self, inner: Callable[..., Any]) -> Callable[..., Any]:
        counts = self

        class Counting:
            def __init__(self, connection: Any) -> None:
                self.connection = connection

            def send_text(self, text: str) -> None:
                counts.add("out", text)
                self.connection.send_text(text)

            def receive_text(self, timeout: float | None = None) -> str | None:
                text = self.connection.receive_text(timeout=timeout)
                if text is not None:
                    counts.add("in", text)
                return text

            def close(self) -> None:
                self.connection.close()

        return lambda config: Counting(inner(config))


def window_bytes(before: Counter[tuple[str, str]], after: Counter[tuple[str, str]], *, kinds: set[str] | None = None) -> int:
    return sum(value - before[key] for key, value in after.items() if kinds is None or key[1] in kinds)


def build_store(state: Path, first_mission: str) -> list[str]:
    """3 running missions (the first one already exists) and 31 finished ones, with large task prompts."""
    running = [first_mission]
    with BridgeStore(state / "bridge.sqlite3") as store:
        created: list[str] = []
        for index in range(RUNNING + FINISHED - 1):
            folder = state.parent / f"project-{index}"
            folder.mkdir()
            created.append(str(store.create_mission(f"Mission {index}", PROMPT, cwd=folder, config={"providers": "codex"})["id"]))
        for index, mission_id in enumerate(created):
            for number in range(12 if index < RUNNING - 1 else 6):
                store.add_task(mission_id, PROMPT, title=f"Task number {number}")
        for number in range(12):
            store.add_task(first_mission, PROMPT, title=f"Task number {number}")
        running += created[: RUNNING - 1]
        for mission_id in running:
            store.db.execute("UPDATE missions SET status='RUNNING' WHERE id=?", (mission_id,))
        for mission_id in created[RUNNING - 1:]:
            store.db.execute("UPDATE tasks SET status='COMPLETED' WHERE mission_id=?", (mission_id,))
            store.db.execute("UPDATE missions SET status='COMPLETED' WHERE id=?", (mission_id,))
    return running


class Events:
    """Appends one event per running mission every ``period`` seconds, like a busy mission would."""

    def __init__(self, state: Path, missions: list[str], period: float) -> None:
        self.state, self.missions, self.period = state, missions, period
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        index = 0
        with BridgeStore(self.state / "bridge.sqlite3") as store:
            while not self.stop.wait(self.period / len(self.missions)):
                mission_id = self.missions[index % len(self.missions)]
                store.record_runtime_event(mission_id, "provider-wait", {"note": "waiting for provider capacity", "i": index})
                index += 1

    def __enter__(self) -> "Events":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self.thread.join(scaled(10))


class Stream:
    """A browser stand-in: reads the coordinator's ``/api/v1/stream`` and keeps every message."""

    def __init__(self, system: Any) -> None:
        coordinator = system.coordinator
        self.client = WebSocketClient.connect(f"wss://localhost:{coordinator.port}/api/v1/stream",
                                              headers={"Authorization": f"Bearer {coordinator.api_token}"}, context=coordinator.context)
        self.messages: list[str] = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                text = self.client.receive_text(timeout=0.2)
            except Exception:  # noqa: BLE001 - closed at the end of the test
                return
            if text is not None:
                self.messages.append(text)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(scaled(5))
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def caught_up(system: Any) -> bool:
    status, body = system.coordinator.request("GET", "/api/v1/workers")
    with BridgeStore(system.state / "bridge.sqlite3", readonly=True) as store:
        last = store.last_event_id()
    return status == 200 and any(item.get("name") == "ws" and item.get("event_cursor") == last for item in body["workers"])


def status_poll(system: Any, mission_id: str) -> dict[str, Any]:
    """What the console sends for a mission's status: GET /api/v1/missions/{id}?worker=ws."""
    status, body = system.coordinator.request("GET", f"/api/v1/missions/{mission_id}?worker=ws&wait_ms=10000")
    assert status == 200 and body["result"]["outcome"] == "ok", body
    return body


@pytest.fixture
def budget_system(tmp_path, monkeypatch, capsys):
    counts = LinkBytes()
    holder: dict[str, list[str]] = {}

    def prepare(state: Path, mission_id: str) -> None:
        holder["running"] = build_store(state, mission_id)

    with running_system(tmp_path, monkeypatch, capsys, allow_control=True, prepare=prepare, connector=counts.connector) as system:
        wait_for(lambda: caught_up(system), timeout=scaled(60), interval=0.2, message="the worker never replicated its history")
        yield system, counts, holder["running"]


def test_steady_state_with_three_running_missions_stays_under_the_budget(budget_system):
    system, counts, running = budget_system
    seconds = scaled(20)
    stream = Stream(system)
    try:
        before, _ = counts.snapshot()
        started = time.monotonic()
        with Events(system.state, running, period=5.0):
            time.sleep(seconds)
            after, _ = counts.snapshot()
            elapsed = time.monotonic() - started
            # The result a console would ask for: one status per running mission.
            for mission_id in running:
                status_poll(system, mission_id)
            polled, _ = counts.snapshot()
            time.sleep(1)
    finally:
        stream.close()
    results = window_bytes(after, polled, kinds={"command.result"}) / len(running)
    rate = window_bytes(before, after) / elapsed
    by_type = {f"{d}/{k}": after[(d, k)] - before[(d, k)] for (d, k) in after if after[(d, k)] - before[(d, k)]}
    print(f"\nsteady state: {rate:.0f} B/s on the worker link, {results:.0f} B per mission.status result; by type {by_type}")
    assert results < STATUS_RESULT_BYTES
    assert rate < STEADY_BYTES_PER_SECOND, by_type

    kinds = {json.loads(text)["type"] for text in stream.messages}
    sizes = [len(text) for text in stream.messages]
    stream_rate = sum(sizes) / (elapsed + 1)
    print(f"browser stream: {len(sizes)} messages, {stream_rate:.0f} B/s, largest {max(sizes)} B, types {sorted(kinds)}")
    assert "event" in kinds
    assert max(sizes) < STREAM_MESSAGE_BYTES
    assert stream_rate < STREAM_BYTES_PER_SECOND
    notice = next(json.loads(text) for text in stream.messages if json.loads(text)["type"] == "event")
    assert "events" not in notice and notice["count"] >= 1 and {"id", "events", "last_event_id", "summary"} <= set(notice["missions"][0])
    assert all("params" not in json.loads(text).get("command", {}).get("request", {}) and "result" not in json.loads(text).get("command", {})
               for text in stream.messages if json.loads(text)["type"] == "command")

    status, diagnostics = system.coordinator.request("GET", "/api/v1/diagnostics")
    link = diagnostics["link_bytes"]
    assert link["worker_in"]["events"]["bytes"] > 0 and link["browser_out"]["event"]["messages"] >= 1


def test_reads_for_an_offline_worker_are_refused_and_a_reconnect_replays_only_writes(budget_system):
    system, counts, running = budget_system
    coordinator = system.coordinator
    system.stop_worker()
    wait_for(lambda: not any(item.get("state") == "online" for item in coordinator.request("GET", "/api/v1/workers")[1]["workers"]),
             timeout=scaled(15), interval=0.1, message="the worker never went offline")

    # What a console does while a worker is away: it keeps asking for the status of its running missions
    # (each GET is a new read command), and a client may post the same read through /commands.
    refused = 0
    for number in range(200):
        status, body = coordinator.request("GET", f"/api/v1/missions/{running[number % RUNNING]}?worker=ws&wait_ms=1000")
        refused += status == 503 and body["error"]["code"] == "internal" and bool(body.get("correlation_id"))
    status, body = coordinator.request("POST", "/api/v1/commands", {"target": "ws", "request": CommandRequest(
        "mission.status", mission_id=running[0], target="ws", request_id="stale-read-posted").to_json()})
    assert status == 503 and body["error"]["code"] == "internal" and body["correlation_id"]
    assert refused == 200, "reads for an offline worker must be refused with the generic 503 problem"

    writes = [f"queued-write-{number}" for number in range(20)]
    for request_id in writes:
        status, body = coordinator.request("POST", "/api/v1/commands", {"target": "ws", "request": CommandRequest(
            "task.add", {"prompt": "A queued task."}, mission_id=running[0], target="ws", request_id=request_id).to_json()})
        assert status == 202 and body["state"] == "pending", body
    status, diagnostics = coordinator.request("GET", "/api/v1/diagnostics")
    assert diagnostics["command_queue_depths"]["ws"] == {"pending": len(writes)}, "only the writes are queued"

    before, messages_before = counts.snapshot()
    started = time.monotonic()
    system.start_worker()
    wait_for(lambda: all(coordinator.request("GET", f"/api/v1/commands/{request_id}")[1].get("state") == "done" for request_id in writes),
             timeout=scaled(30), interval=0.1, message="the queued writes were not replayed")
    wait_for(lambda: caught_up(system), timeout=scaled(30), interval=0.1, message="replication did not catch up")
    time.sleep(scaled(3))
    after, messages_after = counts.snapshot()
    total = window_bytes(before, after)
    results = messages_after[("out", "command.result")] - messages_before[("out", "command.result")]
    print(f"\nreconnect: {total} B in {time.monotonic() - started:.1f} s, {results} command.result messages, "
          f"by type { {f'{d}/{k}': after[(d, k)] - before[(d, k)] for (d, k) in after if after[(d, k)] - before[(d, k)]} }")
    assert results == len(writes), "each queued write is answered once and no read is replayed"
    assert total < RECONNECT_BYTES
    with BridgeStore(system.state / "bridge.sqlite3", readonly=True) as store:
        added = store.db.execute("SELECT COUNT(*) FROM commands WHERE id LIKE 'queued-write-%'").fetchone()[0]
    assert added == len(writes)
