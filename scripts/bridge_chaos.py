#!/usr/bin/env python3
"""Fault injection suite for the distributed Bridge (PRD-008).

Each fault starts its own real ``bridge-coordinator`` and real ``llm-bridge worker run`` processes on
127.0.0.1, injects one failure, and checks the outcome that the fault declares. Every fault states
three expectations, taken from PRD-008:

* no silent loss: a command or event that the system acknowledged is never missing afterwards, and
  nothing is applied twice;
* uncertain outcomes are reported as uncertain: a command whose result was lost stays unfinished
  (``pending``/``sent``/``acked``) or ``uncertain``; it is never reported as ``applied`` without effect;
* recovery without a person: once the fault ends, the system converges by itself (no restart of the
  worker, no manual step), except where the fault declares a documented operator procedure.

    python scripts/bridge_chaos.py --list
    python scripts/bridge_chaos.py                      # every fault
    python scripts/bridge_chaos.py --fault disk-full --fault ws-dropped-frames

The expected outcome of each fault is in ``FAULTS`` below and in docs/distributed/soak-and-chaos.md.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dist_harness as h  # noqa: E402
import dist_ws_proxy  # noqa: E402
from tests.fault_proxy import UP, FaultProxy  # noqa: E402

EXIT_FAILED = 1
EXIT_NO_BINARY = 3
SHIM_SOURCE = Path(__file__).with_name("chaos_clock_shim.c")


class FaultSkipped(Exception):
    """The fault cannot run here; the message is the reason and is printed in the report (never silent)."""


class Expectation(AssertionError):
    """The observed behaviour differs from the fault's declared outcome."""


def expect(condition: object, message: str) -> None:
    if not condition:
        raise Expectation(message)


# --------------------------------------------------------------------------------------------
# Fabric: coordinator + workers (+ one proxy per worker)
# --------------------------------------------------------------------------------------------

@dataclass
class Fabric:
    base: Path
    binary: Path
    workers: int = 2
    link: str = "direct"  # "direct", "tcp" (byte-level fault proxy) or "frames" (TLS-terminating frame proxy)
    coordinator_env: dict[str, str] = field(default_factory=dict)
    ignore_sigxfsz: bool = False
    seed: int = 20261107

    def __post_init__(self) -> None:
        self.coordinator = h.Coordinator(self.binary, self.base / "coordinator", extra_env=self.coordinator_env, ignore_sigxfsz=self.ignore_sigxfsz)
        self.nodes: dict[str, h.WorkerNode] = {}
        self.proxies: dict[str, Any] = {}
        self.ledger = h.Ledger("chaos")
        self.missions: dict[str, str] = {}

    def build(self) -> "Fabric":
        self.coordinator.setup()
        for index in range(self.workers):
            name = f"chaos-w{index + 1}"
            node = h.WorkerNode(name, self.base / name, seed=self.seed + index)
            self.nodes[name] = node
            port, ca = self.coordinator.port, self.coordinator.ca_file
            if self.link == "tcp":
                self.proxies[name] = FaultProxy(self.coordinator.port, seed=self.seed + index)
                port = self.proxies[name].port
            elif self.link == "frames":
                directory = dist_ws_proxy.temp_directory()
                self.proxies[name] = dist_ws_proxy.WsFrameProxy(self.coordinator.port, self.coordinator.ca_file, directory, seed=self.seed + index)
                port, ca = self.proxies[name].port, self.proxies[name].certificate
            node.enroll(self.coordinator, port=port, ca_file=ca)
            self.missions[name] = node.create_mission("idle", run_provider=False)
            node.start()
        self.wait_online()
        return self

    def wait_online(self, names: list[str] | None = None, timeout: float = 120.0) -> None:
        wanted = set(names or self.nodes)
        h.wait_until(lambda: wanted <= {w["name"] for w in self.coordinator.api.workers() if w.get("state") == "online"}, timeout,
                     message=f"workers {sorted(wanted)} online")

    def offline(self, name: str) -> bool:
        return any(w["name"] == name and w["state"] == "offline" for w in self.coordinator.api.workers())

    def submit_tasks(self, count: int, *, concurrency: int = 4, deadline_seconds: float = 90.0, workers: list[str] | None = None,
                     on_progress: Callable[[int], None] | None = None, pause: float = 0.02) -> list[h.Issued]:
        """Issue ``count`` task.add commands; each retries under its own request ID until the coordinator accepts it."""
        names = workers or list(self.nodes)
        rng = random.Random(self.seed)
        issued_list: list[h.Issued] = []
        done = 0
        lock = threading.Lock()
        deadline = time.monotonic() + deadline_seconds
        picks = [rng.choice(names) for _ in range(count)]

        def one(index: int) -> None:
            nonlocal done
            name = picks[index]
            issued, request = self.ledger.add_task_request(name, self.missions[name])
            h.submit_until_accepted(self.coordinator.api, issued, request, deadline=deadline)
            time.sleep(pause)
            with lock:
                issued_list.append(issued)
                done += 1
                if on_progress is not None:
                    on_progress(done)

        with ThreadPoolExecutor(concurrency) as pool:
            list(pool.map(one, range(count)))
        return issued_list

    def command_state(self, request_id: str) -> str | None:
        status, body = self.coordinator.api.get_command(request_id)
        return body.get("state") if status == 200 else None

    def add_events(self, name: str, count: int, tag: str) -> None:
        from llm_tools.bridge_store import BridgeStore

        with BridgeStore(self.nodes[name].db_path) as store:
            for index in range(count):
                store.record_runtime_event(self.missions[name], "link-probe", {"tag": tag, "n": index})

    def events_replicated(self, name: str) -> bool:
        node = self.nodes[name]
        last = node.last_event_id()
        return any(w["name"] == name and w.get("event_cursor") == last for w in self.coordinator.api.workers())

    def _replicated_check(self, name: str) -> Callable[[], bool]:
        return lambda: self.events_replicated(name)

    def assert_events_replicated_exactly(self, timeout: float = 90.0) -> dict[str, int]:
        """After recovery the coordinator holds exactly the events each worker holds: none lost, none invented, none repeated."""
        counts = {}
        for name, node in self.nodes.items():
            h.wait_until(self._replicated_check(name), timeout, message=f"{name} events replicated")
            mine, theirs = node.event_ids(), self.coordinator.api.all_event_ids(name)
            lost, extra = sorted(set(mine) - set(theirs)), sorted(set(theirs) - set(mine))
            expect(not lost and not extra and len(theirs) == len(set(theirs)),
                   f"{name}: events lost {lost[:5]}, invented or duplicated {extra[:5]} ({len(mine)} on the worker, {len(theirs)} on the coordinator)")
            counts[name] = len(theirs)
        return counts

    def verify(self, *, settle: float = 120.0) -> h.Verification:
        report = h.verify_exactly_once(self.ledger, self.coordinator.api, self.nodes, settle_seconds=settle)
        expect(not report.violations, f"command violations: {report.violations[:5]}")
        expect(not report.unfinished, f"commands never finished: {report.unfinished[:5]}")
        return report

    def close(self) -> None:
        for proxy in self.proxies.values():
            with_suppress(proxy.close)
        for node in self.nodes.values():
            with_suppress(node.cleanup)
        with_suppress(self.coordinator.stop)
        for node in self.nodes.values():
            shutil.rmtree(node.runtime, ignore_errors=True)


def with_suppress(function: Callable[[], Any]) -> None:
    try:
        function()
    except Exception:  # noqa: BLE001 - teardown must not hide the test result
        pass


# --------------------------------------------------------------------------------------------
# Faults
# --------------------------------------------------------------------------------------------

def fault_coordinator_restart(fabric: Fabric, *, graceful: bool) -> dict[str, Any]:
    """Restart the coordinator while commands are unfinished: a delayed link keeps several of them in flight."""
    for proxy in fabric.proxies.values():
        proxy.set_delay(0.12, 0.05)  # commands spend a few hundred milliseconds between the journal and the worker's result
    state: dict[str, Any] = {"restarted": False, "in_flight": 0}
    outage: list[float] = []

    def in_flight() -> int:
        depths = fabric.coordinator.api.diagnostics().get("command_queue_depths") or {}
        return sum(sum(per_worker.values()) for per_worker in depths.values())

    def restart_when_in_flight() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not in_flight():
            time.sleep(0.01)
        state["in_flight"] = in_flight()
        started = time.monotonic()
        fabric.coordinator.restart(graceful=graceful)
        outage.append(time.monotonic() - started)

    def maybe_restart(done: int) -> None:
        if done == 20 and not state["restarted"]:
            state["restarted"] = True
            threading.Thread(target=restart_when_in_flight, daemon=True).start()

    issued = fabric.submit_tasks(120, concurrency=8, on_progress=maybe_restart, pause=0.03)
    expect(state["restarted"], "the coordinator was never restarted during the run")
    h.wait_until(lambda: outage, 60, message="coordinator restart to finish")
    expect(state["in_flight"] > 0, "no command was unfinished when the coordinator was restarted; the fault was not injected")
    fabric.wait_online()
    report = fabric.verify()
    retried = sum(1 for item in issued if item.attempts > 1)
    expect(report.accepted == 120, f"only {report.accepted} of 120 commands were accepted by the coordinator")
    events = fabric.assert_events_replicated_exactly()
    return {"commands": report.issued, "outcomes": dict(report.outcomes), "uncertain": len(report.uncertain), "unfinished_at_restart": state["in_flight"],
            "submit_retries_during_outage": retried, "outage_seconds": round(outage[0], 2), "events": events,
            "signal": "SIGTERM" if graceful else "SIGKILL"}


def fault_worker_partition(fabric: Fabric) -> dict[str, Any]:
    name = "chaos-w1"
    proxy: FaultProxy = fabric.proxies[name]
    fabric.submit_tasks(10)
    fabric.add_events(name, 50, "before")
    fabric.assert_events_replicated_exactly()
    proxy.cut()
    h.wait_until(lambda: fabric.offline(name), 30, message="the coordinator to notice the partition")
    expect(fabric.coordinator.api.healthy(), "the coordinator stopped answering while a worker was partitioned")
    _, missions = fabric.coordinator.api.request("GET", "/api/v1/missions")
    mine = [item for item in missions["missions"] if item["worker"] == name]
    expect(mine and all(item["stale"] for item in mine), "missions of a partitioned worker are not marked stale")
    queued = fabric.submit_tasks(20, workers=[name])
    fabric.add_events(name, 600, "during")  # three replication batches produced with no connection
    states = {item.request_id: fabric.command_state(item.request_id) for item in queued}
    expect(all(state in ("pending", "sent") for state in states.values()), f"commands for the partitioned worker were not left unfinished: {set(states.values())}")
    other = fabric.submit_tasks(5, workers=["chaos-w2"])  # the healthy worker is unaffected
    accepted_before = proxy.accepted
    started = time.monotonic()
    proxy.restore()
    h.wait_until(lambda: proxy.accepted > accepted_before and not fabric.offline(name), 150, message="the worker to reconnect after the partition")
    reconnect = time.monotonic() - started
    report = fabric.verify()
    events = fabric.assert_events_replicated_exactly()
    return {"commands": report.issued, "outcomes": dict(report.outcomes), "unfinished_while_partitioned": len(queued), "other_worker_commands": len(other),
            "reconnect_seconds": round(reconnect, 1), "events": events}


def fault_disk_full(fabric: Fabric) -> dict[str, Any]:
    coordinator = fabric.coordinator
    fabric.submit_tasks(10)
    fabric.assert_events_replicated_exactly()
    coordinator.limit_file_size(64 * 1024)  # every write past 64 KiB of any file fails, like a full volume
    statuses: dict[int, int] = {}
    during: list[h.Issued] = []
    for _ in range(12):
        name = "chaos-w1"
        issued, request = fabric.ledger.add_task_request(name, fabric.missions[name])
        issued.attempts += 1
        status, body = coordinator.api.timed_submit(name, request)
        issued.last_status = status
        issued.accepted = status in (200, 202)
        statuses[status] = statuses.get(status, 0) + 1
        during.append(issued)
    expect(coordinator.pid is not None and not coordinator.log_has_panic(), "the coordinator crashed when its volume was full")
    expect(coordinator.api.healthy(), "the coordinator stopped answering health checks while its volume was full")
    # A request answered with 2xx must be durable: it will finish after recovery. Any other answer is a refusal or an unknown outcome.
    refused = [item for item in during if not item.accepted]
    expect(refused, f"no write was refused while every write failed (statuses {statuses}); the fault was not injected")
    expect(all(item.last_status in (0, 500, 503) for item in refused), f"unexpected refusal statuses {statuses}")
    coordinator.limit_file_size(None)  # space is back; nothing is restarted
    fabric.wait_online(timeout=150)
    # A refused command may be retried under its ID: it applies once. Retrying must not duplicate an earlier unknown attempt.
    for item in refused:
        request = {"schema_version": 1, "operation": "task.add", "params": {"prompt": item.marker}, "mission_id": item.mission_id, "target": item.worker,
                   "request_id": item.request_id}
        h.submit_until_accepted(coordinator.api, item, request, deadline=time.monotonic() + 60)
    after = fabric.submit_tasks(20)
    report = fabric.verify()
    events = fabric.assert_events_replicated_exactly()
    return {"statuses_while_full": statuses, "commands": report.issued, "outcomes": dict(report.outcomes), "uncertain": len(report.uncertain),
            "commands_after_recovery": len(after), "events": events}


def build_shim(directory: Path) -> Path:
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        raise FaultSkipped("no C compiler (cc or gcc) to build scripts/chaos_clock_shim.c")
    target = directory / "chaos_clock_shim.so"
    done = subprocess.run([compiler, "-shared", "-fPIC", "-O2", "-o", str(target), str(SHIM_SOURCE), "-ldl"], capture_output=True, text=True)
    if done.returncode != 0:
        raise FaultSkipped(f"building the clock shim failed: {done.stderr.strip()[:200]}")
    return target


def fault_clock_jump(fabric: Fabric, offset_file: Path) -> dict[str, Any]:
    coordinator = fabric.coordinator
    fabric.submit_tasks(10)

    def write_offset(seconds: int) -> None:
        offset_file.write_text(str(seconds))
        time.sleep(0.1)

    def last_seen(name: str) -> float:
        for item in coordinator.api.workers():
            if item["name"] == name:
                return float(item.get("last_seen") or 0)
        return 0.0

    def bounce() -> None:  # a reconnect makes the coordinator stamp the worker's last_seen with its (shifted) clock
        for proxy in fabric.proxies.values():
            proxy.reset_all()
        fabric.wait_online()

    results: dict[str, Any] = {}
    real_before = time.time()
    for label, offset in (("forward", 3600), ("backward", -7200), ("restored", 0)):
        write_offset(offset)
        bounce()
        stamped = last_seen("chaos-w1")
        shift = stamped - time.time()
        results[label] = {"offset_seconds": offset, "last_seen_shift_seconds": round(shift)}
        if label != "restored":
            expect(abs(shift - offset) < 120, f"the clock shim had no effect on the coordinator (last_seen shifted {shift:.0f}s, expected {offset}s)")
        fabric.submit_tasks(15)
        expect(coordinator.api.healthy() and coordinator.pid is not None, f"the coordinator failed after a clock jump {label}")
    report = fabric.verify()
    events = fabric.assert_events_replicated_exactly()
    results.update({"commands": report.issued, "outcomes": dict(report.outcomes), "events": events, "elapsed_real_seconds": round(time.time() - real_before, 1)})
    return results


def fault_ws_slow_frames(fabric: Fabric) -> dict[str, Any]:
    delay = 0.25
    for proxy in fabric.proxies.values():
        proxy.set_delay(dist_ws_proxy.UP, delay)
        proxy.set_delay(dist_ws_proxy.DOWN, delay)
    accepted_before = sum(proxy.accepted for proxy in fabric.proxies.values())
    fabric.add_events("chaos-w1", 300, "slow")
    started = time.monotonic()
    request_id = fabric.ledger.next_id()
    probe = {"schema_version": 1, "operation": "mission.list", "params": {}, "mission_id": None, "target": "chaos-w1", "request_id": request_id}
    status, body = fabric.coordinator.api.submit("chaos-w1", probe, wait_ms=20000)
    roundtrip = time.monotonic() - started
    expect(status == 200 and body.get("state") == "done", f"a command did not finish over a slow link: {status} {body}")
    expect(roundtrip >= 2 * delay * 0.9, f"round trip {roundtrip:.2f}s is shorter than the injected delay; the delay was not applied")
    fabric.submit_tasks(30, concurrency=6)
    report = fabric.verify()
    events = fabric.assert_events_replicated_exactly()
    reconnects = sum(proxy.accepted for proxy in fabric.proxies.values()) - accepted_before
    return {"delay_seconds_each_way": delay, "roundtrip_seconds": round(roundtrip, 2), "commands": report.issued, "outcomes": dict(report.outcomes),
            "reconnects": reconnects, "events": events}


def fault_ws_dropped_frames(fabric: Fabric) -> dict[str, Any]:
    name = "chaos-w1"
    proxy: dist_ws_proxy.WsFrameProxy = fabric.proxies[name]
    observed: dict[str, Any] = {}
    node = fabric.nodes[name]

    def reconnect() -> None:
        accepted = proxy.accepted
        proxy.reset_all()
        h.wait_until(lambda: proxy.accepted > accepted and not fabric.offline(name), 150, message="the worker to reconnect")

    def stuck_for(request_id: str, seconds: float) -> str | None:
        end = time.monotonic() + seconds
        state = fabric.command_state(request_id)
        while time.monotonic() < end:
            state = fabric.command_state(request_id)
            expect(state != "done", f"{request_id} was reported done although a frame was lost")
            time.sleep(0.5)
        return state

    # 1. A command frame is lost on its way to the worker.
    rule = proxy.drop_next(dist_ws_proxy.DOWN, "command")
    first, request = fabric.ledger.add_task_request(name, fabric.missions[name])
    h.submit_until_accepted(fabric.coordinator.api, first, request, deadline=time.monotonic() + 30)
    h.wait_until(lambda: rule.dropped, 10, message="the command frame to be dropped")
    observed["lost_command_state"] = stuck_for(first.request_id, 6.0)
    expect(observed["lost_command_state"] in ("pending", "sent"), f"a lost command is {observed['lost_command_state']}, expected pending or sent")
    expect(node.marker_count(first.marker or "") == 0, "the worker applied a command whose frame was dropped")
    reconnect()
    h.wait_until(lambda: fabric.command_state(first.request_id) == "done", 60, message="the redelivered command to finish")

    # 2. The worker's result frame is lost on its way to the coordinator: the effect exists, the coordinator does not know yet.
    rule = proxy.drop_next(dist_ws_proxy.UP, "command.result")
    second, request = fabric.ledger.add_task_request(name, fabric.missions[name])
    h.submit_until_accepted(fabric.coordinator.api, second, request, deadline=time.monotonic() + 30)
    h.wait_until(lambda: rule.dropped, 10, message="the result frame to be dropped")
    h.wait_until(lambda: node.marker_count(second.marker or "") == 1, 10, message="the worker to apply the command")
    observed["lost_result_state"] = stuck_for(second.request_id, 6.0)
    expect(observed["lost_result_state"] in ("pending", "sent", "acked"), f"a command with a lost result is {observed['lost_result_state']}")
    reconnect()
    h.wait_until(lambda: fabric.command_state(second.request_id) == "done", 60, message="the command with the lost result to finish")
    expect(node.marker_count(second.marker or "") == 1, "redelivery applied the command a second time")

    # 3. An event batch is lost: the next batch fails the cursor check, the coordinator answers events.nack, the worker resends.
    rule = proxy.drop_next(dist_ws_proxy.UP, "events")
    fabric.add_events(name, 30, "dropped-batch")
    h.wait_until(lambda: rule.dropped, 20, message="the event batch to be dropped")
    time.sleep(1.0)
    fabric.add_events(name, 30, "after-gap")
    fabric.assert_events_replicated_exactly()
    observed["nack_recovery_without_reconnect"] = True
    report = fabric.verify()
    observed.update({"dropped": {f"{d}:{t}": n for (d, t), n in proxy.dropped.items()}, "commands": report.issued, "outcomes": dict(report.outcomes)})
    return observed


def _occurrences(data: bytes | bytearray, needle: bytes) -> list[int]:
    found, start = [], 0
    while (position := data.find(needle, start)) >= 0:
        found.append(position)
        start = position + 1
    return found


def fault_db_corrupt_page(fabric: Fabric) -> dict[str, Any]:
    coordinator = fabric.coordinator
    fabric.submit_tasks(40)
    fabric.assert_events_replicated_exactly()
    for node in fabric.nodes.values():
        node.stop()  # no worker writes while the database is replaced
    backup = fabric.base / "backup.sqlite3"
    coordinator.run("backup", "--output", str(backup))
    coordinator.stop()
    # Destroy the b-tree header of the page that stores one command row (the file is closed, so the WAL was checkpointed into it).
    page_size = 4096
    data = bytearray(coordinator.db_path.read_bytes())
    victim = fabric.ledger.mutating()[0]
    needle = b"marker:" + victim.request_id.encode()
    pages = sorted({position // page_size for position in _occurrences(data, needle)})
    expect(pages, "the marker of a stored command was not found in the database file")
    for page in pages:  # the command may also appear in audit or event rows: damage every page that holds it
        data[page * page_size: page * page_size + 8] = b"\xff" * 8
    page = pages[0]
    coordinator.db_path.write_bytes(bytes(data))
    for suffix in ("-wal", "-shm"):
        Path(f"{coordinator.db_path}{suffix}").unlink(missing_ok=True)
    check = subprocess.run([sys.executable, "-c", "import sqlite3,sys\nc=sqlite3.connect(sys.argv[1])\nprint(c.execute('PRAGMA integrity_check').fetchone()[0])", str(coordinator.db_path)],
                           capture_output=True, text=True, env=h.base_environment())
    expect(check.stdout.strip() != "ok", f"the damaged file still passes integrity_check ({check.stdout.strip()!r} {check.stderr.strip()[:100]})")
    observed: dict[str, Any] = {"corrupted_pages": [number + 1 for number in pages]}
    process = subprocess.Popen([str(coordinator.binary), "serve", "--data-dir", str(coordinator.data)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, start_new_session=True, env=h.base_environment())
    try:
        code = process.wait(timeout=20)
        output = (process.stdout.read() if process.stdout else "")[-400:]
        observed.update({"start_result": "refused", "exit_code": code, "message": output.strip().splitlines()[-1] if output.strip() else ""})
        expect(code != 0, "the coordinator started on a corrupt database and exited with status 0")
    except subprocess.TimeoutExpired:
        coordinator.process = process
        try:
            coordinator.wait_healthy(10)
        except RuntimeError:
            pass
        probe_status, _ = coordinator.api.request("GET", "/api/v1/commands/" + victim.request_id)
        observed.update({"start_result": "serving", "probe_status": probe_status, "healthz_while_corrupt": coordinator.api.healthy()})
        expect(probe_status != 200, "the coordinator served a command row from a corrupt page as if it were intact")
        coordinator.stop()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()
    # The documented recovery: restore the backup. Workers then replay anything the backup lacks.
    coordinator.run("restore", "--input", str(backup))
    coordinator.start()
    for node in fabric.nodes.values():
        node.start()
    fabric.wait_online()
    report = fabric.verify()
    events = fabric.assert_events_replicated_exactly()
    observed.update({"restored_commands": report.issued, "outcomes": dict(report.outcomes), "events": events})
    return observed


@dataclass(frozen=True)
class Fault:
    id: str
    title: str
    expected: str
    link: str
    run: Callable[..., dict[str, Any]]
    workers: int = 2
    needs: str = ""


FAULTS: list[Fault] = [
    Fault("coordinator-restart", "Coordinator restarted (SIGTERM) while commands are being delivered",
          "No command is lost or applied twice. Submissions refused during the outage are retried under the same request ID and are accepted afterwards. "
          "Both workers reconnect without a restart and the journal redelivers unfinished commands. Worker events replicate exactly.",
          "tcp", lambda fabric: fault_coordinator_restart(fabric, graceful=True)),
    Fault("coordinator-kill9", "Coordinator killed (SIGKILL) while commands are being delivered",
          "As coordinator-restart. A command whose result was never recorded is redelivered; the worker answers from its stored outcome, so the effect "
          "exists once. An outcome that cannot be confirmed is reported as uncertain, never as applied without an effect.",
          "tcp", lambda fabric: fault_coordinator_restart(fabric, graceful=False)),
    Fault("worker-partition", "Worker network partition and reconnect with replay",
          "The coordinator marks the worker offline and its missions stale within 30 s and keeps answering. Commands for it stay pending or sent (never done). "
          "After the link returns the worker reconnects by itself, queued commands run once, and the events it produced offline replicate exactly.",
          "tcp", fault_worker_partition),
    Fault("disk-full", "Coordinator data volume full",
          "The coordinator process survives and keeps answering health checks. A write that cannot be stored is refused (HTTP 500/503) or fails at the transport; "
          "a command answered with 2xx is durable. When space returns, with no restart, refused commands can be retried under their ID and run once, "
          "workers are online, and events replicate exactly.",
          "tcp", fault_disk_full),
    Fault("clock-jump", "Coordinator wall clock jumps forward one hour, back two hours, then to real time",
          "The coordinator stays up and keeps its worker connections (liveness uses monotonic time). No command is lost or applied twice, event ordering by id "
          "is intact, and the stamps the coordinator writes follow its shifted clock.",
          "tcp", fault_clock_jump, needs="clock-shim"),
    Fault("ws-slow-frames", "Slow WebSocket frames (250 ms in each direction)",
          "Commands finish with a round trip of at least the injected delay, nothing is lost or duplicated, events replicate exactly, and the worker does not reconnect.",
          "frames", fault_ws_slow_frames),
    Fault("ws-dropped-frames", "Dropped WebSocket frames (command, command.result, events)",
          "A lost command or result frame leaves the command unfinished (pending, sent or acked), never done, until the connection is renewed; then it finishes and "
          "applies once. A lost events batch is detected by the next batch (events.nack) and resent without a reconnect.",
          "frames", fault_ws_dropped_frames),
    Fault("db-corrupt-page", "Corrupt page in the coordinator database",
          "The coordinator never serves a command row from the corrupt page as intact: it refuses to start (non-zero exit) or answers the affected request with an "
          "error. Restoring the backup (the documented procedure) brings it back, the workers reconnect, and no command or event is lost.",
          "direct", fault_db_corrupt_page),
]


@dataclass
class FaultResult:
    id: str
    title: str
    expected: str
    status: str  # "pass", "fail" or "skip"
    seconds: float
    observed: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def run_fault(fault: Fault, binary: Path, base: Path) -> FaultResult:
    started = time.monotonic()
    fabric: Fabric | None = None
    try:
        extra_env: dict[str, str] = {}
        offset_file = base / "clock-offset"
        if fault.needs == "clock-shim":
            offset_file.write_text("0")
            extra_env = {"LD_PRELOAD": str(build_shim(base)), "CHAOS_CLOCK_OFFSET_FILE": str(offset_file)}
        fabric = Fabric(base, binary, workers=fault.workers, link=fault.link, coordinator_env=extra_env, ignore_sigxfsz=fault.id == "disk-full").build()
        observed = fault.run(fabric, offset_file) if fault.needs == "clock-shim" else fault.run(fabric)
        return FaultResult(fault.id, fault.title, fault.expected, "pass", time.monotonic() - started, observed)
    except FaultSkipped as exc:
        return FaultResult(fault.id, fault.title, fault.expected, "skip", time.monotonic() - started, reason=str(exc))
    except (Expectation, AssertionError, TimeoutError, RuntimeError) as exc:
        tail = fabric.coordinator.log_tail(8) if fabric else ""
        return FaultResult(fault.id, fault.title, fault.expected, "fail", time.monotonic() - started, {"coordinator_log_tail": tail},
                           reason=f"{type(exc).__name__}: {exc}")
    finally:
        if fabric is not None:
            fabric.close()


# --------------------------------------------------------------------------------------------
# Phase B: faults that need coordinator or protocol features that are not on this branch
# --------------------------------------------------------------------------------------------

PHASE_B: list[dict[str, str]] = [
    {"id": "clock-jump-native", "todo": "TODO(phase B): run clock-jump without the LD_PRELOAD shim once the coordinator reads its wall clock through a test "
     "override (expected: environment variable BRIDGE_COORDINATOR_CLOCK_OFFSET_SECS, re-read on every call), so the musl container build is covered too."},
    {"id": "stale-journal-after-restore", "todo": "TODO(phase B): after PR #31 merges, a restored backup lacks commands issued after it was taken; assert the worker's "
     "stored outcomes are reconciled into the journal (expected endpoint: GET /api/v1/commands?worker=NAME&state=unfinished) instead of staying unknown."},
]


def render_summary(results: list[FaultResult]) -> str:
    lines = ["", "Chaos summary", "============="]
    for item in results:
        lines.append(f"  {item.status.upper():5} {item.id} ({item.seconds:.0f}s) {item.title}")
        if item.status != "pass":
            lines.append(f"        {item.reason}")
        elif item.observed:
            lines.append(f"        observed: {json.dumps(item.observed, default=str)[:400]}")
    for entry in PHASE_B:
        lines.append(f"  TODO  {entry['id']}: {entry['todo']}")
    failed = [item for item in results if item.status == "fail"]
    lines.append("RESULT: " + ("FAIL" if failed else "PASS") + f" ({sum(item.status == 'pass' for item in results)} passed, {len(failed)} failed, "
                 f"{sum(item.status == 'skip' for item in results)} skipped)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coordinator-bin")
    parser.add_argument("--build", action="store_true", help="run cargo build --release when no binary is found")
    parser.add_argument("--fault", action="append", default=[], help="run only this fault (repeatable); see --list")
    parser.add_argument("--list", action="store_true", help="list the faults and their expected outcomes")
    parser.add_argument("--report", type=Path, help="JSON report path (default: chaos-report.json in the current directory)")
    parser.add_argument("--keep", action="store_true", help="keep the run directories")
    parser.add_argument("--allow-skips", action="store_true", help="exit 0 although a fault was skipped (a skip is a failure by default)")
    args = parser.parse_args(argv)
    if args.list:
        for fault in FAULTS:
            print(f"{fault.id}\n  {fault.title}\n  expected: {fault.expected}\n")
        for entry in PHASE_B:
            print(f"{entry['id']}\n  {entry['todo']}\n")
        return 0
    known = {fault.id for fault in FAULTS}
    unknown = [name for name in args.fault if name not in known]
    if unknown:
        print(f"bridge_chaos: unknown fault {unknown}; known: {', '.join(sorted(known))}", file=sys.stderr)
        return 2
    binary = h.find_coordinator_binary(args.coordinator_bin, build=args.build)
    if binary is None:
        print("bridge_chaos: no bridge-coordinator binary; build it (cargo build --release --manifest-path coordinator/Cargo.toml), pass --coordinator-bin or --build", file=sys.stderr)
        return EXIT_NO_BINARY
    results = []
    for fault in FAULTS:
        if args.fault and fault.id not in args.fault:
            continue
        base = h.short_temp_dir(f"chaos-{fault.id[:12]}-")
        print(f"[{time.strftime('%H:%M:%S')}] {fault.id}: {fault.title}", flush=True)
        result = run_fault(fault, binary, base)
        print(f"[{time.strftime('%H:%M:%S')}] {fault.id}: {result.status} in {result.seconds:.0f}s {result.reason}", flush=True)
        results.append(result)
        if result.status == "pass" and not args.keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            print(f"  run directory kept: {base}")
    report = {"tool": "bridge_chaos", "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "binary": str(binary),
              "faults": [vars(item) for item in results], "phase_b": PHASE_B,
              "passed": all(item.status == "pass" or (item.status == "skip" and args.allow_skips) for item in results)}
    path = args.report or Path("chaos-report.json")
    path.write_text(json.dumps(report, indent=1, default=str))
    print(render_summary(results))
    print(f"report: {path}")
    return 0 if report["passed"] else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
