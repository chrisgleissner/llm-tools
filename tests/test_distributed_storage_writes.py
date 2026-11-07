"""Coordinator storage write volume, the microSD wear budget of COORD-003 (docs/distributed/benchmarks.md, "Storage writes").

The coordinator process's own ``write_bytes`` counter (``/proc/<pid>/io``) is read before and
after 1000 replicated events and after 1000 commands round-tripped through a worker. The counter
counts bytes the process caused to be sent to a block device, so the data directory has to be on
one: ``/tmp`` is often a RAM disk, where the counter stays at zero and the test would pass
without measuring anything. ``_block_backed_dir`` finds a directory where a probe write moves
the counter, or the test is skipped.
"""

from __future__ import annotations

import os
import pwd
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from llm_tools import bridge_worker as worker
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled, wait_for
from tests.distributed_harness import requires_coordinator, running_system

pytestmark = [requires_coordinator]

N = 1000
# Measured on an x86-64 host (ext4 on NVMe, release build, 2026-11-05); see benchmarks.md. The
# bounds are about 2.5 times the measured figure, rounded, so that a change that adds a write
# per event (a second index, an audit row, per-event checkpoint) fails and normal variation does not.
EVENTS_WRITE_BYTES_BOUND = 1536 * 1024  # measured 0.54 MiB for 1000 events (545-573 bytes/event, about one database byte written per byte stored)
COMMANDS_WRITE_BYTES_BOUND = 150 * 1024 * 1024  # measured 66-67 MiB for 1000 commands (68 KiB/command: five committed transactions each, a few 4 KiB pages per commit)
DB_GROWTH_BYTES_PER_EVENT_BOUND = 1024  # measured 527-552 bytes/event of file growth; benchmarks.md has 557-558 over 10 000 events
DIAGNOSTICS_GROWTH_BYTES_PER_EVENT_BOUND = 2048
# Idle: a connected worker, pinged every 20 s. Authenticated API polling is deliberately excluded
# because every bearer-token use must become an audit row and is measured by an API contract test.
# Measured 0 bytes over 50 s (2026-11-05). Before the heartbeat throttle (ws_worker.rs
# LAST_SEEN_EVERY_SECS) each ping committed a last_seen update: 24-32 KiB in the same window,
# about 2.2 MiB per worker-hour. The bound allows one small commit (a throttled last_seen or
# last_used touch landing in the window) and nothing per ping.
IDLE_SECONDS = 50
IDLE_WRITE_BYTES_BOUND = 8 * 1024  # db_bytes counts pages not yet checkpointed twice (page_count and the WAL file)


def _write_bytes(pid: int) -> int:
    with open(f"/proc/{pid}/io") as handle:
        for line in handle:
            if line.startswith("write_bytes:"):
                return int(line.split()[1])
    raise AssertionError("no write_bytes line in /proc/<pid>/io")


def _block_backed_dir() -> Path:
    """A fresh directory whose writes reach a block device, judged by this process's own counter."""
    if not Path("/proc/self/io").exists():
        pytest.skip("/proc/<pid>/io is not available on this platform (Linux only)")
    candidates = [os.environ.get("BRIDGE_STORAGE_TEST_DIR"), tempfile.gettempdir(), str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache")]  # tests redirect HOME to a temp directory
    for candidate in filter(None, candidates):
        base = Path(candidate)
        if not base.is_dir():
            continue
        directory = Path(tempfile.mkdtemp(prefix="bridge-storage-", dir=base))
        before = _write_bytes(os.getpid())
        try:
            with open(directory / "probe", "wb") as handle:
                handle.write(os.urandom(1 << 20))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            shutil.rmtree(directory, ignore_errors=True)
            continue
        if _write_bytes(os.getpid()) - before >= 1 << 19:
            return directory
        shutil.rmtree(directory, ignore_errors=True)
    pytest.skip("no block-backed directory for the coordinator data (set BRIDGE_STORAGE_TEST_DIR); tmpfs reports no write_bytes")


def _database_bytes(data: Path) -> int:
    """Size of the coordinator database: main file plus write-ahead log (the shared-memory index is a constant 32 KiB and is left out)."""
    return sum(path.stat().st_size for path in data.iterdir() if path.is_file() and path.name.endswith((".sqlite3", ".sqlite3-wal", ".db", ".db-wal")))


@pytest.fixture
def block_dir():
    directory = _block_backed_dir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _synced(system, store_path: Path) -> None:
    def cursor_matches() -> bool:
        with BridgeStore(store_path, readonly=True) as store:
            last = store.last_event_id()
        status, body = system.coordinator.request("GET", "/api/v1/workers")
        return status == 200 and any(w["name"] == "ws" and w["event_cursor"] == last for w in body["workers"])

    wait_for(cursor_matches, timeout=15, interval=0.05, message="events were not replicated")


def test_one_thousand_replicated_events_stay_within_the_write_and_growth_bounds(tmp_path, monkeypatch, capsys, block_dir):
    # The bounds below were measured with batches of 200 events. The coordinator commits one transaction
    # per batch, and the worker's production batches (25 events, 16 KiB, 20 KiB/s) are deliberately small
    # for slow links, which multiplies the commits and the write-ahead log pages written per event. Pin
    # the batch shape here so that this test keeps measuring the coordinator's cost per stored event.
    monkeypatch.setattr(worker, "EVENT_BATCH", 200)
    monkeypatch.setattr(worker, "EVENT_BATCH_BYTES", 1024 * 1024)
    monkeypatch.setattr(worker, "EVENT_RATE_BYTES_PER_SECOND", 1e9)
    with running_system(tmp_path, monkeypatch, capsys, allow_control=False, data_dir=block_dir / "coordinator") as system:
        store_path = system.state / "bridge.sqlite3"
        _synced(system, store_path)
        pid = system.coordinator.process.pid
        data = system.coordinator.data
        writes_before = _write_bytes(pid)
        status, body = system.coordinator.request("GET", "/api/v1/diagnostics")
        assert status == 200
        db_before = body["db_bytes"]
        files_before = _database_bytes(data)
        assert files_before > 0, [path.name for path in data.iterdir()]
        with BridgeStore(store_path) as store:
            for index in range(N):
                store.record_runtime_event(system.mission_id, "storage-probe", {"n": index, "text": "x" * 100})
        _synced(system, store_path)
        written = _write_bytes(pid) - writes_before
        status, body = system.coordinator.request("GET", "/api/v1/diagnostics")
        files_after = _database_bytes(data)
        growth_files = (files_after - files_before) / N
        growth_logical = (body["db_bytes"] - db_before) / N
        print(f"EVENTS write_bytes={written} ({written / N:.0f}/event) files_growth={growth_files:.0f}/event logical_growth={growth_logical:.0f}/event")
        assert written <= EVENTS_WRITE_BYTES_BOUND, f"{written} bytes written for {N} events"
        assert growth_files <= DB_GROWTH_BYTES_PER_EVENT_BOUND
        assert growth_logical <= DIAGNOSTICS_GROWTH_BYTES_PER_EVENT_BOUND


def test_one_thousand_command_round_trips_stay_within_the_write_bound(tmp_path, monkeypatch, capsys, block_dir):
    with running_system(tmp_path, monkeypatch, capsys, allow_control=False, data_dir=block_dir / "coordinator") as system:
        store_path = system.state / "bridge.sqlite3"
        _synced(system, store_path)
        pid = system.coordinator.process.pid
        writes_before = _write_bytes(pid)
        tokens = system.coordinator.mutation_tokens(34)

        def submit(index: int) -> str:
            request = CommandRequest("mission.list", target="ws", request_id=f"storage-{index}")
            status, body = system.coordinator.request(
                "POST", "/api/v1/commands", {"target": "ws", "request": request.to_json(), "wait_ms": int(scaled(20) * 1000)},
                token=tokens[index % len(tokens)], source_ip=f"127.0.0.{2 + index % 9}",
            )
            assert status == 200, body
            return body["state"]

        with ThreadPoolExecutor(16) as pool:  # one at a time costs about 60 ms each, dominated by the worker loop, not by storage
            states = list(pool.map(submit, range(N)))
        assert states == ["done"] * N
        written = _write_bytes(pid) - writes_before
        print(f"COMMANDS write_bytes={written} ({written / N:.0f}/command)")
        assert written <= COMMANDS_WRITE_BYTES_BOUND, f"{written} bytes written for {N} commands"


def test_an_idle_coordinator_with_a_connected_worker_writes_nothing_per_heartbeat(tmp_path, monkeypatch, capsys, block_dir):
    with running_system(tmp_path, monkeypatch, capsys, allow_control=False, data_dir=block_dir / "coordinator") as system:
        _synced(system, system.state / "bridge.sqlite3")
        pid = system.coordinator.process.pid
        writes_before = _write_bytes(pid)
        deadline = time.monotonic() + IDLE_SECONDS  # spans at least two 20-second worker pings
        while time.monotonic() < deadline:
            time.sleep(5)
        written = _write_bytes(pid) - writes_before
        print(f"IDLE write_bytes={written} over {IDLE_SECONDS} s")
        assert written <= IDLE_WRITE_BYTES_BOUND, f"{written} bytes written in {IDLE_SECONDS} s of idle"
