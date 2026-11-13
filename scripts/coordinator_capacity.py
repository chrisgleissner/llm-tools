#!/usr/bin/env python3
"""Coordinator capacity measurements (PRD-009, docs/distributed/capacity-plan.md).

Drives one coordinator with fake workers, fake browsers and fake terminal producers and prints one JSON
summary per step. Fake clients only: no Bridge worker, no provider, no real state. Use it with a coordinator
on a throw-away data directory, either started here (``--coordinator-bin``) or already running (``--url``,
``--ca``, ``--api-token``; for example the emulated Pi through an SSH port forward).

    nice -n 19 python scripts/coordinator_capacity.py --coordinator-bin coordinator/target/release/bridge-coordinator \
        workers --count 64 --seconds 60

Steps: ``workers`` (N workers, 100 events/s each, commands round trips), ``missions`` (hello with M missions
per worker), ``events`` (offered events/s over W workers), ``browsers`` (N /stream sockets, optional stalled
share), ``terminals`` (S sessions, one browser viewer each, fixed producer rate, optional stalled viewer),
``refusals`` (the caps: workers, enrolment, terminals).
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import shlex
import ssl
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dist_harness as h  # noqa: E402
from llm_tools import bridge_ws  # noqa: E402

HOST = h.HOST
_SMALL = threading.local()
_real_create_connection = bridge_ws.socket.create_connection


def _create_connection(address, timeout=None, *a, **k):  # type: ignore[no-untyped-def]
    """``socket.create_connection``, with a 4 KiB receive buffer when the calling thread asks for a stalled client."""
    if not getattr(_SMALL, "on", False):
        return _real_create_connection(address, timeout, *a, **k)
    import socket as _socket

    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVBUF, 4096)
    sock.settimeout(timeout)
    sock.connect(address)
    return sock


bridge_ws.socket.create_connection = _create_connection
EVENT_TEXT = "x" * 200


def capabilities() -> list[dict[str, Any]]:
    from llm_tools import bridge_commands

    return [spec for spec in bridge_commands.operation_catalog() if not spec["mutating"]]


def pct(values: list[float], fraction: float) -> float:
    return round(h.percentile(values, fraction), 1)


class Probe:
    """Reads the coordinator's RSS, peak RSS, CPU ticks and cgroup memory, locally or through an SSH prefix."""

    def __init__(self, pid: int, prefix: list[str], cgroup: str | None) -> None:
        self.pid, self.prefix, self.cgroup = pid, prefix, cgroup
        self.samples: list[dict[str, float]] = []

    def read(self) -> dict[str, float] | None:
        script = f"grep -E '^(VmRSS|VmHWM|Threads):' /proc/{self.pid}/status; echo STAT; cat /proc/{self.pid}/stat"
        if self.cgroup:
            script += f"; echo CG; cat {self.cgroup}/memory.current {self.cgroup}/memory.peak"
        try:
            out = subprocess.run([*self.prefix, "sh -c " + shlex.quote(script)] if self.prefix else ["sh", "-c", script], capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        sample: dict[str, float] = {"t": time.monotonic()}
        for name, key in (("VmRSS", "rss_mib"), ("VmHWM", "hwm_mib")):
            match = re.search(rf"{name}:\s+(\d+) kB", out)
            if match:
                sample[key] = int(match.group(1)) / 1024
        stat = re.search(r"STAT\n(.*)", out)
        if stat:
            fields = stat.group(1).rsplit(")", 1)[-1].split()
            sample["cpu_ticks"] = int(fields[11]) + int(fields[12])
        cg = out.split("CG\n", 1)[1].split() if "CG\n" in out else []
        if len(cg) >= 2:
            sample["cg_current_mib"], sample["cg_peak_mib"] = int(cg[0]) / 1048576, int(cg[1]) / 1048576
        return sample

    def reset_peak(self) -> None:
        """Reset VmHWM to the current RSS (``clear_refs`` 5), so the peak covers only the measured window."""
        script = f"echo 5 > /proc/{self.pid}/clear_refs"
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run([*self.prefix, "sh -c " + shlex.quote(script)] if self.prefix else ["sh", "-c", script], capture_output=True, timeout=60)

    def run(self, stop: threading.Event, interval: float) -> None:
        while not stop.is_set():
            sample = self.read()
            if sample:
                self.samples.append(sample)
            stop.wait(interval)

    def summary(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if not self.samples:
            return out
        out["rss_peak_mib"] = round(max(s.get("rss_mib", 0) for s in self.samples), 1)
        out["hwm_mib"] = round(max(s.get("hwm_mib", 0) for s in self.samples), 1)
        if any("cg_peak_mib" in s for s in self.samples):
            out["cgroup_peak_mib"] = round(max(s.get("cg_peak_mib", 0) for s in self.samples), 1)
        first, last = self.samples[0], self.samples[-1]
        if "cpu_ticks" in first and last["t"] > first["t"]:
            out["cpu_percent"] = round((last["cpu_ticks"] - first["cpu_ticks"]) / os.sysconf("SC_CLK_TCK") / (last["t"] - first["t"]) * 100, 1)
        return out


class Target:
    def __init__(self, args: argparse.Namespace) -> None:
        self.coordinator: h.Coordinator | None = None
        if args.coordinator_bin:
            data = h.short_temp_dir("capacity-")
            self.coordinator = h.Coordinator(Path(args.coordinator_bin), data / "data", ignore_sigxfsz=True)
            self.coordinator.init()
            config = self.coordinator.data / "config.toml"
            # Every fake client connects from 127.0.0.1, so the 32 connections per address would end the run at 32.
            config.write_text(re.sub(r"(?m)^max_connections_per_ip = \d+$", "max_connections_per_ip = 1000", config.read_text()))
            self.coordinator.start()
            self.coordinator.api_token = self.coordinator.run("token", "create", "harness", "--expires-days", "0", "--scope", "admin")
            self.port, self.ca, self.token = self.coordinator.port, self.coordinator.ca_file, self.coordinator.api_token
            pid = self.coordinator.pid or 0
            prefix: list[str] = []
            self.data = data
        else:
            self.port, self.ca, self.token = args.port, Path(args.ca), args.api_token
            pid, prefix, self.data = args.remote_pid, args.probe_ssh.split() if args.probe_ssh else [], None
        self.probe = Probe(pid, prefix, args.cgroup)
        self.probe_seconds = args.probe_seconds
        self.api = h.ApiClient(self.port, self.ca, self.token)
        self.context = ssl.create_default_context(cafile=str(self.ca))
        self._n = 0

    def enrol(self, name: str) -> str:
        status, body = self.api.request("POST", "/api/v1/workers", {"name": name})
        if status == 409 and body.get("code") == "worker-exists":  # a coordinator that is reused between steps
            self.api.request("DELETE", f"/api/v1/workers/{name}")
            status, body = self.api.request("POST", "/api/v1/workers", {"name": name})
        if status != 201:
            raise RuntimeError(f"enrol {name}: {status} {body}")
        return str(body["token"])

    def url(self, path: str) -> str:
        return f"wss://{HOST}:{self.port}{path}"

    def stop(self) -> None:
        if self.coordinator:
            self.coordinator.stop()
        if self.data and os.environ.get("CAPACITY_KEEP"):
            print(f"kept {self.data}", file=sys.stderr)
        elif self.data:
            subprocess.run(["rm", "-rf", str(self.data)], check=False)


class FakeWorker(threading.Thread):
    """Completes hello/welcome, answers commands, sends event batches at a fixed rate, opens terminal producers on request."""

    def __init__(self, target: Target, name: str, *, missions: int = 1, rate: float = 0.0, batch: int = 100, term_rate: int = 0) -> None:
        super().__init__(daemon=True)
        self.t, self.name_, self.n_missions, self.rate, self.batch, self.term_rate = target, name, missions, rate, batch, term_rate
        self.token = target.enrol(name)
        self.ws: bridge_ws.WebSocketClient | None = None
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.ready = threading.Event()
        self.generation = 0
        self.seq = 0
        self.cursor = 0
        self.on_send: Any = None
        self.born = time.monotonic()
        self.close_at = 0.0
        self.errors: list[str] = []
        self.nacks = 0
        self.events_sent = 0
        self.close: tuple[int, str] | None = None
        self.terminal_threads: list[threading.Thread] = []
        self.stalled_terminals: set[str] = set()

    def message(self, kind: str, payload: dict[str, Any]) -> str:
        self.seq += 1
        return json.dumps({"v": 1, "type": kind, "id": f"{self.name_}-{self.seq}", "worker_id": f"wid-{self.name_}", "generation": self.generation, "payload": payload})

    def send(self, kind: str, payload: dict[str, Any]) -> None:
        with self.lock:
            assert self.ws is not None
            self.ws.send_text(self.message(kind, payload))

    def connect(self) -> bool:
        try:
            self.ws = bridge_ws.WebSocketClient.connect(self.t.url("/api/v1/worker/connect"), headers={"Authorization": f"Bearer {self.token}"}, context=self.t.context, timeout=30)
        except (OSError, bridge_ws.WebSocketError) as exc:
            self.errors.append(f"connect: {exc}")
            return False
        # About 300 bytes each, like a real summary (protocol.md, Mission summaries).
        missions = [{"id": f"{self.name_}-m{i}", "name": f"mission {i} parser cleanup", "status": "RUNNING", "folder": "project-dir", "updated_at": "2026-11-13T00:00:00+00:00",
                     "task_counts": {"COMPLETED": 36, "QUEUED": 4}, "current_task": {"ordinal": 37, "title": "Add the retry path to the scheduler and cover it with a test"},
                     "station": "claude", "model": "claude-sonnet-5", "needs_kind": None, "needs_tasks": 0} for i in range(self.n_missions)]
        self.send("hello", {"worker_id": f"wid-{self.name_}", "name": self.name_, "software_version": "capacity", "protocol_versions": [1], "platform": {"system": "Linux"},
                            "capabilities": capabilities(), "missions": missions, "event_cursor": 0})
        return True

    def welcome(self) -> bool:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                text = self.ws.receive_text(timeout=5) if self.ws else None
            except (OSError, bridge_ws.WebSocketError) as exc:
                self.errors.append(f"welcome: {exc}")
                if isinstance(exc, bridge_ws.WebSocketClosed):
                    self.close = (exc.code, exc.reason)
                return False
            if text is None:
                continue
            message = json.loads(text)
            if message["type"] == "welcome":
                self.generation = message["payload"]["generation"]
                self.ready.set()
                return True
            if message["type"] == "error":
                self.errors.append(f"{message['payload']['code']}: {message['payload']['message']}")
        return False

    def run(self) -> None:
        if not self.connect() or not self.welcome():
            self.ready.set()
            return
        writer = threading.Thread(target=self.write_events, daemon=True)
        writer.start()
        while not self.stop_flag.is_set():
            try:
                text = self.ws.receive_text(timeout=1.0)
            except bridge_ws.WebSocketClosed as exc:
                self.close = (exc.code, exc.reason)
                self.close_at = time.monotonic() - self.born
                return
            except (OSError, bridge_ws.WebSocketError) as exc:
                self.errors.append(f"read: {exc}")
                return
            if text is None:
                continue
            message = json.loads(text)
            kind, payload = message["type"], message["payload"]
            if kind == "command":
                self.send("command.ack", {"request_id": payload["request_id"], "status": "received"})
                self.send("command.result", {"schema_version": 1, "request_id": payload["request_id"], "operation": payload["operation"], "outcome": "ok", "result": {"items": []}, "error": None})
            elif kind == "events.nack":
                self.nacks += 1
            elif kind == "error":
                self.errors.append(f"{payload.get('code')}")
            elif kind == "terminal.open":
                thread = threading.Thread(target=self.terminal, args=(payload["session_id"],), daemon=True)
                thread.start()
                self.terminal_threads.append(thread)
            elif kind == "ping":
                self.send("pong", {})

    def send_events(self, count: int, message: str) -> float:
        """Send ``count`` events with the next ids as one batch and return the send time. Ids are assigned under the send lock."""
        with self.lock:
            assert self.ws is not None
            events = [{"id": self.cursor + i + 1, "kind": "task-progress", "mission_id": f"{self.name_}-m0", "message": message, "ts": time.time()} for i in range(count)]
            started = time.monotonic()
            self.ws.send_text(self.message("events", {"after": self.cursor, "through": self.cursor + count, "events": events}))
            self.cursor += count
            self.events_sent += count
            if self.on_send:
                self.on_send(self.cursor, started)
            return started

    def write_events(self) -> None:
        if self.rate <= 0:
            return
        period = self.batch / self.rate
        next_at = time.monotonic()
        while not self.stop_flag.is_set():
            next_at += period
            try:
                self.send_events(self.batch, EVENT_TEXT)
            except (OSError, bridge_ws.WebSocketError):
                return
            delay = next_at - time.monotonic()
            if delay > 0:
                self.stop_flag.wait(delay)

    def probe_event(self) -> float:
        """Send one event and return the monotonic send time."""
        return self.send_events(1, "probe")

    def terminal(self, session_id: str) -> None:
        try:
            ws = bridge_ws.WebSocketClient.connect(self.t.url(f"/api/v1/worker/terminal/{session_id}"), headers={"Authorization": f"Bearer {self.token}"}, context=self.t.context, timeout=30)
        except (OSError, bridge_ws.WebSocketError) as exc:
            self.errors.append(f"terminal connect: {exc}")
            return
        try:
            ws.send_text(json.dumps({"t": "s", "cols": 100, "rows": 30, "mode": "view", "reason": ""}))
            chunk = max(1, self.term_rate // 20)
            seq = 0
            while not self.stop_flag.is_set():
                seq += 1
                data = struct.pack(">dI", time.time(), seq) + b"y" * max(0, chunk - 12)
                ws.send_text(json.dumps({"t": "o", "d": base64.b64encode(data).decode()}))
                self.stop_flag.wait(0.05)
        except (OSError, bridge_ws.WebSocketError):
            pass
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass


def start_workers(target: Target, count: int, **kw: Any) -> list[FakeWorker]:
    workers = [FakeWorker(target, f"cw{i}", **kw) for i in range(count)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.ready.wait(90)
    return workers


def stop_workers(workers: list[FakeWorker]) -> None:
    for worker in workers:
        worker.stop_flag.set()
    for worker in workers:
        worker.join(5)
        if worker.ws:
            try:
                worker.ws.close()
            except Exception:  # noqa: BLE001
                pass


def run_window(target: Target, seconds: float, body) -> dict[str, float]:
    stop = threading.Event()
    thread = threading.Thread(target=target.probe.run, args=(stop, target.probe_seconds), daemon=True)
    target.probe.samples.clear()
    thread.start()
    try:
        body()
    finally:
        stop.set()
        thread.join(30)
    return target.probe.summary()


def roundtrips(target: Target, workers: list[FakeWorker], seconds: float, rate: float, wait_ms: int = 5000) -> tuple[list[float], int]:
    latencies: list[float] = []
    errors = 0
    api = h.ApiClient(target.port, target.ca, target.token)
    end = time.monotonic() + seconds
    index = 0
    interval = 1.0 / rate
    while time.monotonic() < end:
        worker = workers[index % len(workers)]
        index += 1
        request = {"schema_version": 1, "operation": "mission.list", "params": {}, "mission_id": None, "target": worker.name_, "request_id": f"rt-{index}-{int(time.time() * 1000)}"}
        started = time.monotonic()
        status, body = api.submit(worker.name_, request, wait_ms=wait_ms)
        if status == 200 and body.get("state") == "done":
            latencies.append((time.monotonic() - started) * 1000)
        else:
            errors += 1
            if errors == 1:
                print(f"first command failure: {status} {str(body)[:300]}", file=sys.stderr)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))
    return latencies, errors


def step_workers(target: Target, args: argparse.Namespace) -> dict[str, Any]:
    workers = start_workers(target, args.count, missions=10, rate=100.0, batch=10)
    connected = sum(1 for w in workers if w.generation)
    result: dict[str, Any] = {"step": "workers", "n": args.count, "connected": connected}

    def body() -> None:
        time.sleep(args.warmup)
        for w in workers:
            w.errors.clear()
        latencies, errors = roundtrips(target, workers, args.seconds, args.command_rate, args.wait_ms)
        result.update(roundtrip_p50_ms=pct(latencies, 0.5), roundtrip_p95_ms=pct(latencies, 0.95), roundtrip_p99_ms=pct(latencies, 0.99), command_errors=errors, commands=len(latencies) + errors)

    result.update(run_window(target, args.seconds, body))
    result["worker_errors"] = sum(len(w.errors) for w in workers)
    result["nacks"] = sum(w.nacks for w in workers)
    result["closed"] = sum(1 for w in workers if w.close)
    result["diagnostics"] = target.api.diagnostics().get("resources")
    stop_workers(workers)
    return result


def step_missions(target: Target, args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"step": "missions", "workers": args.count, "per_worker": args.missions, "total": args.count * args.missions}
    hello_ms: list[float] = []
    workers: list[FakeWorker] = []

    def body() -> None:
        for i in range(args.count):
            worker = FakeWorker(target, f"cm{i}", missions=args.missions)
            started = time.monotonic()
            worker.start()
            worker.ready.wait(120)
            hello_ms.append((time.monotonic() - started) * 1000)
            workers.append(worker)
        after_hello = target.probe.read() or {}
        result.update(hwm_after_hello_mib=round(after_hello.get("hwm_mib", 0), 1), rss_after_hello_mib=round(after_hello.get("rss_mib", 0), 1))
        api = target.api
        paged, unpaged = [], []
        for _ in range(20):
            started = time.monotonic()
            api.request("GET", "/api/v1/missions?limit=100")
            paged.append((time.monotonic() - started) * 1000)
        for _ in range(3):
            started = time.monotonic()
            status, body_ = api.request("GET", "/api/v1/missions")
            unpaged.append((time.monotonic() - started) * 1000)
        after_lists = target.probe.read() or {}
        result.update(hwm_after_lists_mib=round(after_lists.get("hwm_mib", 0), 1), rss_after_lists_mib=round(after_lists.get("rss_mib", 0), 1))
        result.update(hello_p50_ms=pct(hello_ms, 0.5), hello_max_ms=pct(hello_ms, 1.0), list_paged_p95_ms=pct(paged, 0.95), list_default_p95_ms=pct(unpaged, 0.95),
                      refused=[w.errors[-1] for w in workers if w.errors][:3], connected=sum(1 for w in workers if w.generation))

    result.update(run_window(target, 0, body))
    result["diagnostics"] = {k: v for k, v in target.api.diagnostics().items() if k in ("db_bytes", "missions_indexed", "enrolled_workers")}
    stop_workers(workers)
    time.sleep(3)
    closed = target.probe.read() or {}
    result["rss_after_disconnect_mib"] = round(closed.get("rss_mib", 0), 1)
    return result


def step_events(target: Target, args: argparse.Namespace) -> dict[str, Any]:
    per_worker = args.rate / args.count
    batch = int(min(250, max(10, per_worker / 10)))
    workers = start_workers(target, args.count, rate=per_worker, batch=batch)
    result: dict[str, Any] = {"step": "events", "offered_per_s": args.rate, "workers": args.count, "batch": batch}
    browsers = start_browsers(target, args.browsers) if args.browsers else []

    def body() -> None:
        time.sleep(args.warmup)
        base = sum(w.events_sent for w in workers)
        started = time.monotonic()
        visibility: list[float] = []
        end = started + args.seconds
        while time.monotonic() < end:
            time.sleep(2)
            probe_worker = workers[0]
            sent = probe_worker.probe_event()
            while time.monotonic() - sent < 10:
                status, body_ = target.api.request("GET", f"/api/v1/events?worker={probe_worker.name_}&after={probe_worker.cursor - 1}&limit=1")
                if status == 200 and body_.get("events"):
                    visibility.append((time.monotonic() - sent) * 1000)
                    break
                time.sleep(0.05)
        elapsed = time.monotonic() - started
        result.update(achieved_per_s=round((sum(w.events_sent for w in workers) - base) / elapsed), visibility_p50_ms=pct(visibility, 0.5), visibility_p99_ms=pct(visibility, 0.99))

    result.update(run_window(target, args.seconds, body))
    result["nacks"] = sum(w.nacks for w in workers)
    result["worker_errors"] = sum(len(w.errors) for w in workers)
    result["closed"] = sum(1 for w in workers if w.close)
    result["close_reasons"] = sorted({f"{w.close[0]} {w.close[1]} after {w.close_at:.0f}s ({w.name_})" for w in workers if w.close})
    result["worker_error_kinds"] = sorted({e for w in workers for e in w.errors})[:3]
    diag = target.api.diagnostics()
    result["db_bytes_mib"] = round(diag.get("db_bytes", 0) / 1048576, 1)
    result["diagnostics"] = diag.get("resources")
    stop_workers(workers)
    stop_browsers(browsers)
    return result


class Browser(threading.Thread):
    def drain(self) -> None:
        self.close = drain(self.ws)

    def __init__(self, target: Target, stalled: bool = False) -> None:
        super().__init__(daemon=True)
        self.t, self.stalled = target, stalled
        self.stop_flag = threading.Event()
        self.ready = threading.Event()
        self.close: tuple[int, str] | None = None
        self.error = ""
        self.received = 0
        self.fanout: list[float] = []
        self.ws: bridge_ws.WebSocketClient | None = None
        self.sent_at: dict[tuple[str, int], float] = {}

    def run(self) -> None:
        _SMALL.on = self.stalled
        try:
            self.ws = bridge_ws.WebSocketClient.connect(self.t.url("/api/v1/stream"), headers={"Authorization": f"Bearer {self.t.token}"}, context=self.t.context, timeout=30)
        except (OSError, bridge_ws.WebSocketError) as exc:
            self.error = str(exc)
            self.ready.set()
            return
        self.ready.set()
        if self.stalled:
            self.stop_flag.wait()  # the socket is open but nothing reads it until drain()
            self.drain()
            return
        while not self.stop_flag.is_set():
            try:
                text = self.ws.receive_text(timeout=1.0)
            except bridge_ws.WebSocketClosed as exc:
                self.close = (exc.code, exc.reason)
                return
            except (OSError, bridge_ws.WebSocketError) as exc:
                self.error = str(exc)
                return
            if text is not None:
                self.received += 1
                message = json.loads(text)
                if message.get("type") == "event":
                    sent = self.sent_at.get((message["worker"], message["cursor"]))
                    if sent:
                        self.fanout.append((time.monotonic() - sent) * 1000)


def drain(client: bridge_ws.WebSocketClient | None) -> tuple[int, str] | None:
    """Read a stalled client's socket until the server's close frame shows up (None when the socket stays open)."""
    end = time.monotonic() + 8
    while client and time.monotonic() < end:
        try:
            client.receive_text(timeout=1.0)
        except bridge_ws.WebSocketClosed as exc:
            return exc.code, exc.reason
        except (OSError, bridge_ws.WebSocketError):
            return (0, "reset")
    return None


def start_browsers(target: Target, count: int, stalled: int = 0) -> list[Browser]:
    browsers = [Browser(target, stalled=i < stalled) for i in range(count)]
    for browser in browsers:
        browser.start()
    for browser in browsers:
        browser.ready.wait(60)
    return browsers


def stop_browsers(browsers: list[Browser]) -> None:
    for browser in browsers:
        browser.stop_flag.set()
    for browser in browsers:
        browser.join(5)
        if browser.ws:
            try:
                browser.ws.close()
            except Exception:  # noqa: BLE001
                pass


def step_browsers(target: Target, args: argparse.Namespace) -> dict[str, Any]:
    workers = start_workers(target, 8, rate=args.publish_rate, batch=1)
    browsers = start_browsers(target, args.count, stalled=args.stalled)
    shared: dict[tuple[str, int], float] = {}
    for browser in browsers:
        browser.sent_at = shared
    result: dict[str, Any] = {"step": "browsers", "n": args.count, "stalled": args.stalled, "open": sum(1 for b in browsers if not b.error)}
    for w in workers:
        w.on_send = lambda cursor, when, name=w.name_: shared.__setitem__((name, cursor), when)

    def body() -> None:
        time.sleep(args.warmup)
        time.sleep(args.seconds)

    result.update(run_window(target, args.seconds, body))
    live = [b for b in browsers if not b.stalled]
    fan = [v for b in live for v in b.fanout]
    result.update(fanout_p50_ms=pct(fan, 0.5), fanout_p99_ms=pct(fan, 0.99), live_closed=sum(1 for b in live if b.close), connect_errors=sorted({b.error for b in browsers if b.error})[:3])
    result["diagnostics"] = target.api.diagnostics().get("resources")
    stop_workers(workers)
    for b in browsers:
        b.stop_flag.set()
    for b in browsers:
        b.join(12)
    closed = [b.close for b in browsers if b.stalled and b.close]
    result.update(stalled_closed_count=len(closed), stalled_closed=closed[:2])
    stop_browsers(browsers)
    return result


class Viewer(threading.Thread):
    def __init__(self, target: Target, worker: str, mission: str, stalled: bool) -> None:
        super().__init__(daemon=True)
        self.t, self.worker, self.mission, self.stalled = target, worker, mission, stalled
        self.stop_flag = threading.Event()
        self.ready = threading.Event()
        self.lat: list[float] = []
        self.frames = 0
        self.gaps = 0
        self.close: tuple[int, str] | None = None
        self.refused = ""
        self.error = ""
        self.ws: bridge_ws.WebSocketClient | None = None

    def run(self) -> None:
        query = f"worker={self.worker}&mission={self.mission}&mode=view&cols=100&rows=30"
        _SMALL.on = self.stalled
        try:
            self.ws = bridge_ws.WebSocketClient.connect(self.t.url(f"/api/v1/terminal?{query}"), headers={"Authorization": f"Bearer {self.t.token}"}, context=self.t.context, timeout=30)
        except (OSError, bridge_ws.WebSocketError) as exc:
            self.error = str(exc)
            self.ready.set()
            return
        self.ready.set()
        last = 0
        while not self.stop_flag.is_set():
            if self.stalled:
                self.stop_flag.wait()
                self.close = drain(self.ws)
                return
            try:
                text = self.ws.receive_text(timeout=1.0)
            except bridge_ws.WebSocketClosed as exc:
                self.close = (exc.code, exc.reason)
                return
            except (OSError, bridge_ws.WebSocketError) as exc:
                self.error = str(exc)
                return
            if text is None:
                continue
            frame = json.loads(text)
            if frame["t"] == "x":
                self.refused = frame.get("reason", "")
            elif frame["t"] == "o":
                raw = base64.b64decode(frame["d"])
                if len(raw) >= 12:
                    sent, seq = struct.unpack(">dI", raw[:12])
                    self.lat.append((time.time() - sent) * 1000)
                    if last and seq != last + 1:
                        self.gaps += 1
                    last = seq
                self.frames += 1


def step_terminals(target: Target, args: argparse.Namespace) -> dict[str, Any]:
    per_worker = 16
    worker_count = max(1, -(-args.sessions // per_worker))
    workers = start_workers(target, worker_count, term_rate=args.rate_kib * 1024)
    viewers: list[Viewer] = []
    for i in range(args.sessions):
        worker = workers[i // per_worker]
        viewers.append(Viewer(target, worker.name_, f"{worker.name_}-m0", stalled=i < args.stalled))
    result: dict[str, Any] = {"step": "terminals", "sessions": args.sessions, "producer_kib_s": args.rate_kib, "stalled": args.stalled}

    def body() -> None:
        for v in viewers:
            v.start()
            time.sleep(0.02)
        for v in viewers:
            v.ready.wait(60)
        time.sleep(args.warmup)
        for v in viewers:
            v.lat.clear()
        time.sleep(args.seconds)

    result.update(run_window(target, args.seconds, body))
    live = [v for v in viewers if not v.stalled]
    lat = [x for v in live for x in v.lat]
    result.update(relay_p50_ms=pct(lat, 0.5), relay_p99_ms=pct(lat, 0.99), frames=sum(v.frames for v in live), gaps=sum(v.gaps for v in live), live_closed=sum(1 for v in live if v.close),
                  live_refused=[v.refused for v in live if v.refused][:2], errors=sorted({v.error for v in viewers if v.error})[:2])
    result["diagnostics"] = target.api.diagnostics().get("resources")
    for v in viewers:
        v.stop_flag.set()
    for v in viewers:
        v.join(12)
    result["stalled_closed"] = [v.close for v in viewers if v.stalled and v.close][:2]
    stop_workers(workers)
    return result


def step_refusals(target: Target, args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {"step": "refusals"}
    workers = start_workers(target, 64)
    result["workers_connected"] = sum(1 for w in workers if w.generation)
    extra = FakeWorker(target, "cw-extra")
    extra.start()
    extra.ready.wait(30)
    result["worker_65_connected"] = bool(extra.generation)
    result["worker_65_errors"] = extra.errors[:2]
    stop_workers(workers + [extra])
    return result


STEPS = {"workers": step_workers, "missions": step_missions, "events": step_events, "browsers": step_browsers, "terminals": step_terminals, "refusals": step_refusals}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coordinator-bin", help="start this coordinator on a throw-away data directory")
    parser.add_argument("--port", type=int, help="remote coordinator port (with --ca and --api-token)")
    parser.add_argument("--ca")
    parser.add_argument("--api-token")
    parser.add_argument("--probe-ssh", help="command prefix that runs a shell on the coordinator host, for /proc and cgroup reads")
    parser.add_argument("--remote-pid", type=int, default=0)
    parser.add_argument("--cgroup", help="cgroup directory of the coordinator (memory.current and memory.peak)")
    parser.add_argument("--out", type=Path, help="append the JSON summary here")
    parser.add_argument("--host-note", default="", help="free text recorded with the result (host, build)")
    parser.add_argument("--probe-seconds", type=float, default=2.0, help="seconds between resource samples (use 10 or more through SSH to an emulated host)")
    parser.add_argument("--warmup", type=float, default=10.0)
    parser.add_argument("--seconds", type=float, default=60.0)
    sub = parser.add_subparsers(dest="step", required=True)
    for name in STEPS:
        sub.add_parser(name)
    sub.choices["workers"].add_argument("--count", type=int, default=16)
    sub.choices["workers"].add_argument("--command-rate", type=float, default=20.0)
    sub.choices["workers"].add_argument("--wait-ms", type=int, default=5000, help="how long a command POST waits for the result (at most 30000)")
    sub.choices["missions"].add_argument("--count", type=int, default=8)
    sub.choices["missions"].add_argument("--missions", type=int, default=1000)
    sub.choices["events"].add_argument("--rate", type=float, default=500.0)
    sub.choices["events"].add_argument("--count", type=int, default=8)
    sub.choices["events"].add_argument("--browsers", type=int, default=0)
    sub.choices["browsers"].add_argument("--count", type=int, default=64)
    sub.choices["browsers"].add_argument("--stalled", type=int, default=0)
    sub.choices["browsers"].add_argument("--publish-rate", type=float, default=8.0, help="event batches per second per fake worker (8 workers)")
    sub.choices["terminals"].add_argument("--sessions", type=int, default=16)
    sub.choices["terminals"].add_argument("--rate-kib", type=int, default=16)
    sub.choices["terminals"].add_argument("--stalled", type=int, default=0)
    args = parser.parse_args(argv)
    if not args.coordinator_bin and not (args.port and args.ca and args.api_token):
        parser.error("give --coordinator-bin, or --port, --ca and --api-token")
    target = Target(args)
    try:
        target.probe.reset_peak()  # hwm_mib is then the peak of this step, not of the start-up
        result = STEPS[args.step](target, args)
    finally:
        target.stop()
    result["host"] = args.host_note
    result["date"] = time.strftime("%Y-%m-%d", time.gmtime())
    line = json.dumps(result, sort_keys=True)
    print(line)
    if args.out:
        with args.out.open("a") as handle:
            handle.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
