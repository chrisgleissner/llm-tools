"""Shared harness for the distributed soak (PRD-007) and chaos (PRD-008) runs.

Everything here is standard library only. It starts a real ``bridge-coordinator`` binary on 127.0.0.1
with a throw-away data directory, enrols real ``llm-bridge worker run`` processes (each with its own
state, runtime, config and home directory), drives missions with the fake provider
``scripts/bridge_soak_provider.py``, issues commands with unique request IDs and checks afterwards that
each one was applied exactly once, samples the coordinator's resource use, and computes slopes and
latency percentiles.

Safety rules the harness enforces for every child process it starts:

* the environment is built from scratch (no inherited ``LLM_*`` variables, ``HOME`` inside the
  harness directory), so the operator's Bridge state, provider credentials and config are never read;
* provider capacity is always injected (``LLM_SCHEDULER_USAGE_JSON_FILE``), never read live;
* ``LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1`` and ``LLM_TOOLS_NO_INHIBIT=1`` keep a rate-limit wait from
  suspending the machine;
* the coordinator and the fault proxies bind 127.0.0.1 only.
"""

from __future__ import annotations

import array
import contextlib
import http.client
import json
import math
import os
import random
import re
import resource
import shlex
import signal
import socket
import sqlite3
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
for _path in (str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

PROVIDER_SCRIPT = SCRIPTS / "bridge_soak_provider.py"
LLM_BRIDGE = ROOT / "llm-bridge"
HOST = "127.0.0.1"
DB_NAME = "coordinator.sqlite3"
WORKER_DB_NAME = "bridge.sqlite3"
MAX_EVENTS_PER_WORKER = 50_000  # coordinator default `max_events_per_worker`


# --------------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------------

def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile (``fraction`` in 0..1); 0.0 for an empty sequence."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


def least_squares(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Slope and intercept of the least-squares line through ``points`` (x in seconds)."""
    count = len(points)
    if count < 2:
        return 0.0, points[0][1] if points else 0.0
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    spread = sum((x - mean_x) ** 2 for x, _ in points)
    if spread == 0:
        return 0.0, mean_y
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
    return slope, mean_y - slope * mean_x


@dataclass(frozen=True)
class SlopeBound:
    """A growth limit for one sampled metric.

    ``per_hour`` is the largest acceptable least-squares slope, in the metric's unit per hour.
    ``floor`` is the smallest fitted rise over the measured window that counts as growth at all, so
    a few units of allocator or cache noise in a short run is not extrapolated into a failure.
    """

    per_hour: float
    floor: float
    unit: str
    per_process: bool = False  # the numbers are for one process; multiply by the process count of the fleet


# Documented in docs/distributed/soak-and-chaos.md. A run fails a metric when its slope over the
# post-warm-up window is above ``per_hour`` AND the fitted rise over that window is above ``floor``.
DEFAULT_BOUNDS: dict[str, SlopeBound] = {
    "coordinator_rss_kib": SlopeBound(per_hour=8 * 1024, floor=4 * 1024, unit="KiB"),
    "coordinator_fds": SlopeBound(per_hour=4, floor=4, unit="descriptors"),
    "coordinator_threads": SlopeBound(per_hour=2, floor=4, unit="threads"),
    "coordinator_db_bytes": SlopeBound(per_hour=32 * 1024 * 1024, floor=8 * 1024 * 1024, unit="bytes"),
    "workers_rss_kib": SlopeBound(per_hour=512, floor=1024, unit="KiB", per_process=True),
    "workers_fds": SlopeBound(per_hour=1, floor=1, unit="descriptors", per_process=True),
    "workers_threads": SlopeBound(per_hour=1, floor=1, unit="threads", per_process=True),
    "controllers_rss_kib": SlopeBound(per_hour=1536, floor=512, unit="KiB", per_process=True),
    "controllers_fds": SlopeBound(per_hour=1, floor=1, unit="descriptors", per_process=True),
}


# Largest value any single sample may have, whatever the slope: a process that settles at a size it should never
# reach is a failure even when it stops growing. The ``*_max_rss_kib`` series are the largest single process of the fleet.
DEFAULT_CEILINGS: dict[str, tuple[float, str]] = {
    "controllers_max_rss_kib": (56 * 1024, "KiB"),
    "workers_max_rss_kib": (56 * 1024, "KiB"),
    "coordinator_rss_kib": (96 * 1024, "KiB"),
}


def scale_bounds(bounds: dict[str, SlopeBound], *, workers: int, controllers: int) -> dict[str, SlopeBound]:
    """Per-process bounds multiplied by the number of worker or controller processes of this run."""
    scaled: dict[str, SlopeBound] = {}
    for metric, bound in bounds.items():
        count = controllers if metric.startswith("controllers_") else workers
        factor = count if bound.per_process else 1
        scaled[metric] = SlopeBound(bound.per_hour * factor, bound.floor * factor, bound.unit)
    return scaled


# The database must also be explained by its rows: size <= BASE + rows * BYTES_PER_ROW at every sample.
DB_BASE_BYTES = 8 * 1024 * 1024  # the write-ahead log alone reaches 4 MiB before SQLite checkpoints it
DB_BYTES_PER_ROW = 4096
# The coordinator keeps an index of its stored events, so its memory grows with the row count until the retention cap.
# Measured on a debug build: 96 bytes per row (21.9 to 33.2 MiB for 3.6k to 125k rows); the allowance is 2.7 times that.
COORDINATOR_RSS_BYTES_PER_ROW = 256

# p99 budgets in milliseconds per request class (loopback, one coordinator).
DEFAULT_LATENCY_BUDGET_MS: dict[str, float] = {
    "read": 250.0,  # GET /api/v1/{missions,workers,diagnostics,commands/...}
    "submit": 500.0,  # POST /api/v1/commands without wait_ms
    "roundtrip": 10_000.0,  # POST /api/v1/commands with wait_ms, answered after the worker's result
}


@dataclass
class SlopeResult:
    metric: str
    samples: int
    window_seconds: float
    slope_per_hour: float
    fitted_rise: float
    bound_per_hour: float
    floor: float
    unit: str
    verdict: str  # "pass", "fail" or "inconclusive"
    detail: str = ""


def check_slope(metric: str, points: Sequence[tuple[float, float]], bound: SlopeBound, *, warmup_fraction: float = 0.25,
                min_samples: int = 4, explained: Callable[[float, float], float] | None = None, warmup_seconds: float | None = None) -> SlopeResult:
    """Fit a line through the samples after the warm-up part of the run and compare it with ``bound``.

    The warm-up is ``warmup_seconds`` from the first sample when that is given, else ``warmup_fraction`` of the run.

    ``explained(first, last)`` gives growth that the workload accounts for between two sample times
    (for the database: the new rows times a per-row budget); it is added to the floor.
    """
    if not points:
        return SlopeResult(metric, 0, 0.0, 0.0, 0.0, bound.per_hour, bound.floor, bound.unit, "inconclusive", "no samples")
    start = points[0][0] + (warmup_seconds if warmup_seconds is not None else (points[-1][0] - points[0][0]) * warmup_fraction)
    window = [point for point in points if point[0] >= start]
    if len(window) < min_samples:
        return SlopeResult(metric, len(window), 0.0, 0.0, 0.0, bound.per_hour, bound.floor, bound.unit, "inconclusive",
                           f"{len(window)} samples after warm-up, need {min_samples}")
    slope, _ = least_squares(window)
    span = window[-1][0] - window[0][0]
    rise = slope * span
    per_hour = slope * 3600.0
    floor = bound.floor + (explained(window[0][0], window[-1][0]) if explained else 0.0)
    grows = per_hour > bound.per_hour and rise > floor
    detail = f"slope {per_hour:.1f}/h, fitted rise {rise:.1f} over {span:.0f}s (bound {bound.per_hour:.1f}/h above {floor:.1f})"
    return SlopeResult(metric, len(window), span, per_hour, rise, bound.per_hour, floor, bound.unit, "fail" if grows else "pass", detail)


def check_per_process_slope(metric: str, series: dict[str, list[tuple[float, float]]], bound: SlopeBound, *, warmup_seconds: float,
                            min_samples: int = 4) -> SlopeResult:
    """The median over processes of the slope after each process's own warm-up, for a fleet whose members come and go.

    A summed series jumps whenever a controller is stopped, started again or exits, and a restarted process starts a
    new warm-up. Each process is fitted on its own, from ``warmup_seconds`` after its first sample. A leak shows in the
    median of the fleet; one process with an odd step does not decide the verdict. A process counts as growing when its
    slope is above ``bound.per_hour`` and its fitted rise above ``bound.floor``; the verdict fails when the median
    slope is above the bound and the median rise above the floor. Both are per process.
    """
    slopes: list[float] = []
    rises: list[float] = []
    for points in series.values():
        if not points:
            continue
        window = [point for point in points if point[0] >= points[0][0] + warmup_seconds]
        if len(window) < min_samples:
            continue
        slope, _ = least_squares(window)
        slopes.append(slope * 3600.0)
        rises.append(slope * (window[-1][0] - window[0][0]))
    if not slopes:
        return SlopeResult(metric, 0, 0.0, 0.0, 0.0, bound.per_hour, bound.floor, bound.unit, "inconclusive",
                           f"no process has {min_samples} samples after its {warmup_seconds:.0f}s warm-up")
    slopes.sort()
    rises.sort()
    median_slope, median_rise = statistics.median(slopes), statistics.median(rises)
    detail = (f"median slope {median_slope:.1f}/h and rise {median_rise:.1f} per process over {len(slopes)} of {len(series)} processes "
              f"(bound {bound.per_hour:.1f}/h above {bound.floor:.1f}; slowest {slopes[0]:.1f}/h, fastest {slopes[-1]:.1f}/h)")
    grows = median_slope > bound.per_hour and median_rise > bound.floor
    return SlopeResult(metric, len(slopes), 0.0, median_slope, median_rise, bound.per_hour, bound.floor, bound.unit, "fail" if grows else "pass", detail)


def check_ceiling(metric: str, points: Sequence[tuple[float, float]], ceiling: float, unit: str) -> SlopeResult:
    """The largest sample against ``ceiling``; the warm-up is included."""
    if not points:
        return SlopeResult(f"{metric} ceiling", 0, 0.0, 0.0, 0.0, 0.0, ceiling, unit, "inconclusive", "no samples")
    peak_time, peak = max(points, key=lambda point: point[1])
    return SlopeResult(f"{metric} ceiling", len(points), points[-1][0] - points[0][0], 0.0, 0.0, 0.0, ceiling, unit,
                       "fail" if peak > ceiling else "pass", f"peak {peak:.0f} {unit} at {peak_time:.0f}s (ceiling {ceiling:.0f})")


MAX_WORKER_BACKLOG = 100  # unfinished commands of one worker: at the soak's 2 commands per second over 10 workers, 8 minutes behind


def check_command_backlog(samples: Sequence[dict[str, Any]], limit: int = MAX_WORKER_BACKLOG) -> list[str]:
    """Samples where one worker has more unfinished commands (any state except ``done``, from ``command_queue_depths``) than ``limit``."""
    problems = []
    for sample in samples:
        for worker, states in (sample.get("queue_depths") or {}).items():
            backlog = sum(int(count) for count in states.values())
            if backlog > limit:
                problems.append(f"t={sample['t']:.0f}s: {worker} has {backlog} unfinished commands (limit {limit})")
    return problems


def check_database_explained_by_rows(samples: Sequence[dict[str, Any]], *, base: int = DB_BASE_BYTES, per_row: int = DB_BYTES_PER_ROW) -> list[str]:
    """Samples where the coordinator database is larger than its row count can explain."""
    problems = []
    for sample in samples:
        rows = sum(int(value) for value in (sample.get("rows") or {}).values())
        size = int(sample.get("db_bytes") or 0)
        if size > base + rows * per_row:
            problems.append(f"t={sample['t']:.0f}s: database {size} bytes for {rows} rows (limit {base + rows * per_row})")
    return problems


def check_latency(recorder: "LatencyRecorder", budgets: dict[str, float]) -> list[dict[str, Any]]:
    """p50/p99/max per request class with a pass/fail against the p99 budget."""
    results = []
    for label, budget in budgets.items():
        values = recorder.values(label)
        p99 = percentile(values, 0.99) * 1000.0
        results.append({
            "class": label, "count": len(values), "p50_ms": round(percentile(values, 0.5) * 1000.0, 2), "p99_ms": round(p99, 2),
            "max_ms": round(max(values, default=0.0) * 1000.0, 2), "budget_p99_ms": budget,
            "verdict": "pass" if p99 <= budget else "fail", "errors": recorder.errors(label),
        })
    return results


class LatencyRecorder:
    """Thread-safe request durations grouped by request class."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, array.array] = {}
        self._errors: Counter[str] = Counter()

    def add(self, label: str, seconds: float) -> None:
        with self._lock:
            self._values.setdefault(label, array.array("d")).append(seconds)

    def error(self, label: str) -> None:
        with self._lock:
            self._errors[label] += 1

    def values(self, label: str) -> list[float]:
        with self._lock:
            return list(self._values.get(label, ()))

    def errors(self, label: str) -> int:
        with self._lock:
            return self._errors[label]


# --------------------------------------------------------------------------------------------
# Processes and /proc
# --------------------------------------------------------------------------------------------

def proc_stats(pid: int | None) -> dict[str, int] | None:
    """``{"rss_kib", "fds", "threads"}`` of a live process from /proc, or ``None`` when it is gone."""
    if not pid:
        return None
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        fds = len(os.listdir(f"/proc/{pid}/fd"))
    except (OSError, ValueError):
        return None
    values = {}
    for line in status.splitlines():
        if line.startswith(("VmRSS:", "Threads:")):
            name, rest = line.split(":", 1)
            values[name] = int(rest.split()[0])
    return {"rss_kib": values.get("VmRSS", 0), "fds": fds, "threads": values.get("Threads", 0)}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def find_coordinator_binary(explicit: str | None = None, *, build: bool = False) -> Path | None:
    """``--coordinator-bin``, ``BRIDGE_COORDINATOR_BIN``, then coordinator/target/{release,debug}; optionally ``cargo build --release``."""
    candidates = [explicit, os.environ.get("BRIDGE_COORDINATOR_BIN")]
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            return path if path.exists() else None
    for profile in ("release", "debug"):
        path = ROOT / "coordinator" / "target" / profile / "bridge-coordinator"
        if path.exists():
            return path
    if build:
        subprocess.run(["cargo", "build", "--locked", "--release", "--manifest-path", str(ROOT / "coordinator" / "Cargo.toml")], check=True)
        path = ROOT / "coordinator" / "target" / "release" / "bridge-coordinator"
        return path if path.exists() else None
    return None


def short_temp_dir(prefix: str) -> Path:
    """A private directory with a short path (Unix socket paths have a length limit)."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    path.chmod(0o700)
    return path


def base_environment() -> dict[str, str]:
    """A minimal clean environment: PATH and locale only. No ``LLM_*``, ``TMUX`` or credentials."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": os.environ.get("LANG", "C.UTF-8"), "TERM": "dumb"}
    for name in ("SYSTEMROOT", "TMPDIR"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


# --------------------------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------------------------

class ApiClient:
    """Keep-alive HTTPS client for the coordinator API (one connection per thread)."""

    def __init__(self, port: int, ca_file: Path, token: str, recorder: LatencyRecorder | None = None) -> None:
        self.port = port
        self.token = token
        self.recorder = recorder
        self._context = ssl.create_default_context(cafile=str(ca_file))
        self._local = threading.local()

    def _connection(self, timeout: float) -> http.client.HTTPSConnection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = http.client.HTTPSConnection(HOST, self.port, context=self._context, timeout=timeout)
            self._local.connection = connection
        connection.timeout = timeout
        return connection

    def _drop_connection(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
        self._local.connection = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 30.0, label: str | None = None) -> tuple[int, dict[str, Any]]:
        """``(status, json)``; a transport failure returns ``(0, {"error": ...})`` after one reconnect attempt."""
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        started = time.perf_counter()
        for attempt in (1, 2):
            try:
                connection = self._connection(timeout)
                connection.request(method, path, body=payload, headers=headers)
                response = connection.getresponse()
                raw = response.read()
                if response.getheader("Connection", "").lower() == "close":
                    self._drop_connection()
                data = json.loads(raw) if raw else {}
                if label and self.recorder is not None:
                    self.recorder.add(label, time.perf_counter() - started)
                return response.status, data if isinstance(data, dict) else {"value": data}
            except (OSError, http.client.HTTPException, ValueError) as exc:
                self._drop_connection()
                if attempt == 2:
                    if label and self.recorder is not None:
                        self.recorder.error(label)
                    return 0, {"error": f"{type(exc).__name__}: {exc}"}
        return 0, {"error": "unreachable"}

    def submit(self, target: str, request: dict[str, Any], *, wait_ms: int = 0) -> tuple[int, dict[str, Any]]:
        return self.request("POST", "/api/v1/commands", {"target": target, "request": request, "wait_ms": wait_ms}, timeout=30.0 + wait_ms / 1000.0)

    def timed_submit(self, target: str, request: dict[str, Any], *, wait_ms: int = 0) -> tuple[int, dict[str, Any]]:
        label = "roundtrip" if wait_ms else "submit"
        started = time.perf_counter()
        status, body = self.submit(target, request, wait_ms=wait_ms)
        elapsed = time.perf_counter() - started
        if self.recorder is not None:
            # A round trip is only meaningful when the worker answered inside the wait.
            if status == 0:
                self.recorder.error(label)
            elif not wait_ms or (status == 200 and body.get("state") == "done"):
                self.recorder.add(label, elapsed)
        return status, body

    def get_command(self, request_id: str) -> tuple[int, dict[str, Any]]:
        return self.request("GET", f"/api/v1/commands/{request_id}", label="read")

    def healthy(self) -> bool:
        return self.request("GET", "/healthz", timeout=3.0)[0] == 200

    def workers(self) -> list[dict[str, Any]]:
        status, body = self.request("GET", "/api/v1/workers", label="read")
        return list(body.get("workers", [])) if status == 200 else []

    def diagnostics(self) -> dict[str, Any]:
        status, body = self.request("GET", "/api/v1/diagnostics", label="read")
        return body if status == 200 else {}

    def events_after(self, worker: str, after: int, limit: int = 1000) -> list[dict[str, Any]]:
        status, body = self.request("GET", f"/api/v1/events?worker={worker}&after={after}&limit={limit}")
        return list(body.get("events", [])) if status == 200 else []

    def all_event_ids(self, worker: str) -> list[int]:
        ids: list[int] = []
        while True:
            batch = self.events_after(worker, max(ids, default=0))
            if not batch:
                return ids
            ids.extend(int(item["id"]) for item in batch)


# --------------------------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------------------------

@dataclass
class TerminalOpen:
    """One remote terminal opened through the coordinator, timed from the browser's request to the first screen."""

    seconds_to_first_output: float | None
    seconds_to_status: float | None
    frames: int
    error: str = ""


def open_terminal(coordinator: "Coordinator", worker: str, mission_id: str, *, mode: str = "view", cols: int = 100, rows: int = 30,
                  limit: float = 15.0, hold: float = 2.0) -> TerminalOpen:
    """Open ``/api/v1/terminal`` as an API client and wait for the first ``o`` frame (the screen snapshot), then read for ``hold`` seconds.

    The clock starts before the WebSocket upgrade request and stops when the first output frame has arrived, so the figure includes
    the coordinator's ``terminal.open`` to the worker, the worker's own connection to the coordinator and the first snapshot.
    """
    from llm_tools import bridge_ws

    context = ssl.create_default_context(cafile=str(coordinator.ca_file))
    query = f"worker={worker}&mission={mission_id}&mode={mode}&cols={cols}&rows={rows}"
    started = time.monotonic()
    first = status = None
    frames = 0
    try:
        client = bridge_ws.WebSocketClient.connect(f"wss://{HOST}:{coordinator.port}/api/v1/terminal?{query}",
                                                   headers={"Authorization": f"Bearer {coordinator.api_token}"}, context=context, timeout=limit)
    except (OSError, bridge_ws.WebSocketError) as exc:
        return TerminalOpen(None, None, 0, f"{type(exc).__name__}: {exc}")
    try:
        end = started + limit
        while time.monotonic() < end and (first is None or time.monotonic() < first + started + hold):
            try:
                text = client.receive_text(timeout=max(0.05, min(0.5, end - time.monotonic())))
            except (OSError, bridge_ws.WebSocketError) as exc:
                return TerminalOpen(first, status, frames, f"{type(exc).__name__}: {exc}")
            if text is None:
                continue
            frames += 1
            try:
                kind = json.loads(text).get("t")
            except ValueError:
                kind = None
            elapsed = time.monotonic() - started
            if kind == "s" and status is None:
                status = elapsed
            if kind == "o" and first is None:
                first = elapsed
            if kind == "x":
                return TerminalOpen(first, status, frames, "session ended: " + text[:120])
        return TerminalOpen(first, status, frames, "" if first is not None else f"no output within {limit:g}s")
    finally:
        with contextlib.suppress(Exception):
            client.close()


def _ignore_sigxfsz() -> None:
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past RLIMIT_FSIZE fails with EFBIG instead of killing the process


class Coordinator:
    """One ``bridge-coordinator serve`` process on a throw-away data directory, restartable on the same port."""

    def __init__(self, binary: Path, data_dir: Path, *, port: int | None = None, extra_env: dict[str, str] | None = None,
                 ignore_sigxfsz: bool = False) -> None:
        self.binary = binary
        self.data = data_dir
        self.port = port or free_port()
        self.extra_env = dict(extra_env or {})
        self.ignore_sigxfsz = ignore_sigxfsz
        self.log_path = data_dir.parent / f"{data_dir.name}.log"
        self.process: subprocess.Popen[Any] | None = None
        self.api_token = ""
        self.recorder = LatencyRecorder()
        self._api: ApiClient | None = None

    # -- lifecycle
    def run(self, *args: str, timeout: float = 60.0) -> str:
        done = subprocess.run([str(self.binary), *args, "--data-dir", str(self.data)], check=True, capture_output=True, text=True, timeout=timeout,
                              env={**base_environment(), **self.extra_env})
        return done.stdout.strip()

    def init(self) -> None:
        self.data.parent.mkdir(parents=True, exist_ok=True)
        self.run("init", "--hostname", "localhost")
        config = self.data / "config.toml"
        text = re.sub(r'bind = ".*"', f'bind = "{HOST}:{self.port}"', config.read_text())
        # The load generators submit far more mutations than the production budgets (30 per token, 120 per address
        # per minute) allow; raise only those two budgets to their maximum (DECISIONS.md D12).
        text = re.sub(r"(?m)^mutation_limit_per_(token|address) = \d+$", r"mutation_limit_per_\1 = 10000", text)
        config.write_text(text)

    @property
    def ca_file(self) -> Path:
        return self.data / "ca.pem"

    @property
    def db_path(self) -> Path:
        return self.data / DB_NAME

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process and self.process.poll() is None else None

    def start(self, *, wait: bool = True, timeout: float = 30.0) -> None:
        log = open(self.log_path, "ab")
        try:
            self.process = subprocess.Popen(
                [str(self.binary), "serve", "--data-dir", str(self.data)], stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                env={**base_environment(), **self.extra_env}, preexec_fn=_ignore_sigxfsz if self.ignore_sigxfsz else None,
            )
        finally:
            log.close()
        if wait:
            self.wait_healthy(timeout)

    def wait_healthy(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        probe = ApiClient(self.port, self.ca_file, self.api_token or "none")
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"coordinator exited with {self.process.returncode}: {self.log_tail()}")
            if probe.healthy():
                return
            time.sleep(0.1)
        raise RuntimeError(f"coordinator did not become healthy within {timeout}s: {self.log_tail()}")

    def setup(self) -> None:
        """Init, start, create the API token."""
        self.init()
        self.start()
        self.api_token = self.run("token", "create", "harness", "--expires-days", "0", "--scope", "admin")

    def add_worker(self, name: str) -> str:
        return self.run("worker", "add", name)

    def stop(self, timeout: float = 15.0) -> int | None:
        """SIGTERM and wait; the coordinator drains connections and exits (10 s bound)."""
        process = self.process
        if process is None or process.poll() is not None:
            return process.returncode if process else None
        os.killpg(process.pid, signal.SIGTERM)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            return process.wait(timeout=5)

    def kill9(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)

    def restart(self, *, graceful: bool = True) -> None:
        if graceful:
            self.stop()
        else:
            self.kill9()
        self.start()

    def log_tail(self, lines: int = 20) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def log_has_panic(self) -> bool:
        try:
            return bool(re.search(r"panicked at|stack backtrace|SIGSEGV", self.log_path.read_text(errors="replace")))
        except OSError:
            return False

    # -- observation
    @property
    def api(self) -> ApiClient:
        if self._api is None or self._api.token != self.api_token:
            self._api = ApiClient(self.port, self.ca_file, self.api_token, self.recorder)
        return self._api

    def stats(self) -> dict[str, int] | None:
        return proc_stats(self.pid)

    def db_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal"):
            with contextlib.suppress(OSError):
                total += os.stat(f"{self.db_path}{suffix}").st_size
        return total

    def row_counts(self, tables: Iterable[str] = ("events", "commands", "audit")) -> dict[str, int]:
        counts: dict[str, int] = {}
        try:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
        except sqlite3.Error:
            return counts
        try:
            for table in tables:
                with contextlib.suppress(sqlite3.Error):
                    counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608 - fixed table names
        finally:
            connection.close()
        return counts

    def audit_submissions(self) -> Counter[str]:
        """How many ``command.submit`` audit entries exist per request ID (a new command is audited once; a repeated POST adds none)."""
        counts: Counter[str] = Counter()
        try:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            return counts
        try:
            for (detail,) in connection.execute("SELECT detail FROM audit WHERE action='command.submit'"):
                match = re.search(r"request_id=(\S+)", detail)
                if match:
                    counts[match.group(1)] += 1
        except sqlite3.Error:
            pass
        finally:
            connection.close()
        return counts

    def limit_file_size(self, limit: int | None) -> None:
        """Set (or with ``None`` lift) ``RLIMIT_FSIZE`` of the running process: the disk-full fault."""
        if self.pid is None:
            raise RuntimeError("coordinator is not running")
        soft = resource.RLIM_INFINITY if limit is None else limit
        resource.prlimit(self.pid, resource.RLIMIT_FSIZE, (soft, resource.RLIM_INFINITY))


# --------------------------------------------------------------------------------------------
# Workers
# --------------------------------------------------------------------------------------------

def scan_processes(runtime_dirs: Iterable[Path], *, only_controllers: bool = False) -> dict[str, list[int]]:
    """Processes grouped by the ``XDG_RUNTIME_DIR`` of their environment, from one pass over /proc.

    The harness gives every worker a private runtime directory that its controllers and their provider
    processes inherit, so the directory identifies everything that belongs to a worker. This process is excluded.
    """
    wanted = {f"XDG_RUNTIME_DIR={path}".encode(): str(path) for path in runtime_dirs}
    found: dict[str, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            if only_controllers and b"_controller" not in Path(f"/proc/{entry}/cmdline").read_bytes():
                continue
            environ = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for item in environ:
            if item in wanted:
                found.setdefault(wanted[item], []).append(int(entry))
    return found


def scan_controllers(runtime_dirs: Iterable[Path]) -> dict[str, list[int]]:
    """Mission controller processes grouped by runtime directory."""
    return scan_processes(runtime_dirs, only_controllers=True)


def usage_payload(*, limited_for: float = 0.0, remaining: int = 80) -> dict[str, Any]:
    """The injected provider capacity: healthy, or with the 5h window used up until ``limited_for`` seconds from now."""
    now = int(time.time())
    healthy = {"remaining": remaining, "resets_at": now + 5 * 3600}
    if limited_for > 0:
        window = {"remaining": 0, "resets_at": now + max(1, int(limited_for))}
    else:
        window = healthy
    return {"codex": {"available": True, "five_hour": window, "week": {"remaining": 60, "resets_at": now + 3 * 86400}}}


@dataclass
class ProviderMix:
    """Weights of the fake provider's behaviours (see ``scripts/bridge_soak_provider.py``)."""

    output: float = 80
    question: float = 5
    ratelimit: float = 10
    crash: float = 5

    def text(self) -> str:
        return f"{self.output:g},{self.question:g},{self.ratelimit:g},{self.crash:g}"


class WorkerNode:
    """One enrolled worker: its own state, runtime, config and home directories and a ``worker run`` process."""

    def __init__(self, name: str, base: Path, *, seed: int = 0, mix: ProviderMix | None = None, speed: float = 1.0,
                 retry_delays: str = "2,4", question_idle: int = 6, idle_timeout: int = 20, extra_env: dict[str, str] | None = None) -> None:
        self.name = name
        self.extra_env = dict(extra_env or {})
        self.base = base
        self.state = base / "state"
        self.runtime = short_temp_dir(f"lbw-{name}-")
        self.home = base / "home"
        self.config_home = base / "config"
        self.usage_file = base / "usage.json"
        self.seed = seed
        self.mix = mix or ProviderMix()
        self.speed = speed
        self.question_idle = question_idle
        self.idle_timeout = idle_timeout
        self.retry_delays = retry_delays
        self.process: subprocess.Popen[bytes] | None = None
        self.log_path = base / "worker.log"
        self.missions: list[str] = []
        for directory in (self.state, self.home, self.config_home / "llm-tools"):
            directory.mkdir(parents=True, exist_ok=True)
        self.state.chmod(0o700)
        (self.config_home / "llm-tools" / "config.toml").write_text(f"[ralph]\nretry_delays = \"{self.retry_delays}\"\n")
        self.set_usage(usage_payload())

    # -- environment and CLI
    def env(self, **extra: str) -> dict[str, str]:
        env = base_environment()
        env.update({
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config_home), "XDG_RUNTIME_DIR": str(self.runtime),
            "XDG_STATE_HOME": str(self.base / "xdg-state"), "XDG_CACHE_HOME": str(self.base / "cache"),
            "LLM_BRIDGE_STATE_DIR": str(self.state), "LLM_SCHEDULER_USAGE_JSON_FILE": str(self.usage_file),
            "LLM_SCHEDULER_NO_ACTUAL_SUSPEND": "1", "LLM_TOOLS_NO_INHIBIT": "1", "LLM_USAGE_NO_SERVICE": "1",
            "LLM_BRIDGE_NO_SIGNIN_CHECK": "1", "LLM_BRIDGE_NO_OSC": "1", "LLM_TOOLS_LOCAL_BLOCK_BACKOFF": "5",
            "LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT": str(self.question_idle), "LLM_SCHEDULER_IDLE_TIMEOUT": str(self.idle_timeout),
            "SOAK_PROVIDER_SEED": str(self.seed), "SOAK_PROVIDER_MIX": self.mix.text(), "SOAK_PROVIDER_SPEED": str(self.speed),
        })
        env.update(self.extra_env)
        env.update(extra)
        return env

    def cli(self, *args: str, timeout: float = 60.0, check: bool = True, **env: str) -> subprocess.CompletedProcess[str]:
        done = subprocess.run([sys.executable, str(LLM_BRIDGE), *args], env=self.env(**env), capture_output=True, text=True, timeout=timeout, cwd=self.base)
        if check and done.returncode != 0:
            raise RuntimeError(f"llm-bridge {' '.join(args)} failed ({done.returncode}): {done.stderr.strip() or done.stdout.strip()}")
        return done

    def set_usage(self, payload: dict[str, Any]) -> None:
        temporary = self.usage_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload))
        os.replace(temporary, self.usage_file)

    # -- enrolment and the worker process
    def enroll(self, coordinator: Coordinator, *, port: int | None = None, ca_file: Path | None = None) -> None:
        token = coordinator.add_worker(self.name)
        token_file = self.base / "worker-token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        self.cli("worker", "enroll", "--name", self.name, "--coordinator", f"wss://{HOST}:{port or coordinator.port}", "--token-file", str(token_file),
                 "--ca-file", str(ca_file or coordinator.ca_file), "--allow-control")

    def start(self) -> None:
        log = open(self.log_path, "ab")
        try:
            self.process = subprocess.Popen([sys.executable, str(LLM_BRIDGE), "worker", "run"], env=self.env(), stdout=log, stderr=log,
                                            stdin=subprocess.DEVNULL, start_new_session=True, cwd=self.base)
        finally:
            log.close()

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process and self.process.poll() is None else None

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def kill9(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)

    # -- missions
    def create_mission(self, label: str, *, prompt: str | None = None, run_provider: bool = True, mode: str = "headless") -> str:
        """Create a mission. ``mode="background"`` gives it a tmux station, which a remote terminal needs."""
        folder = self.base / "projects" / label
        folder.mkdir(parents=True, exist_ok=True)
        args = ["mission", "create", "--prompt", prompt or f"soak mission {label}", "--cwd", str(folder), "--providers", "codex", "--mode", mode,
                "--new", "--json"]
        if run_provider:
            args += ["--verifier", "true", "--command-template", shlex.join([sys.executable, str(PROVIDER_SCRIPT)])]
        created = json.loads(self.cli(*args).stdout)
        mission_id = str(created["mission"]["id"])
        self.missions.append(mission_id)
        return mission_id

    def start_mission(self, mission_id: str) -> None:
        self.cli("start", "--mission", mission_id)

    def stop_missions(self) -> None:
        for mission_id in self.missions:
            self.cli("stop", "--mission", mission_id, check=False, timeout=30)

    # -- worker database (read-only)
    @property
    def db_path(self) -> Path:
        return self.state / WORKER_DB_NAME

    @contextlib.contextmanager
    def database(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        try:
            yield connection
        finally:
            connection.close()

    def marker_count(self, marker: str) -> int:
        with self.database() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM tasks WHERE original_prompt=?", (marker,)).fetchone()[0])

    def command_status(self, request_id: str) -> list[str]:
        with self.database() as connection:
            return [row[0] for row in connection.execute("SELECT status FROM commands WHERE id=?", (request_id,))]

    def task_statuses(self) -> Counter[str]:
        try:
            with self.database() as connection:
                return Counter({row[0]: row[1] for row in connection.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")})
        except sqlite3.Error:
            return Counter()

    def mission_statuses(self) -> Counter[str]:
        try:
            with self.database() as connection:
                return Counter({row[0]: row[1] for row in connection.execute("SELECT status, COUNT(*) FROM missions GROUP BY status")})
        except sqlite3.Error:
            return Counter()

    def operation_outcomes(self) -> Counter[str]:
        try:
            with self.database() as connection:
                return Counter({str(row[0]): row[1] for row in connection.execute("SELECT exit_code, COUNT(*) FROM operations GROUP BY exit_code")})
        except sqlite3.Error:
            return Counter()

    def event_ids(self, exclude_kinds: Sequence[str] = ()) -> list[int]:
        with self.database() as connection:
            marks = ",".join("?" for _ in exclude_kinds)
            query = "SELECT id FROM events" + (f" WHERE kind NOT IN ({marks})" if exclude_kinds else "") + " ORDER BY id"
            return [int(row[0]) for row in connection.execute(query, tuple(exclude_kinds))]

    def last_event_id(self) -> int:
        with self.database() as connection:
            return int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

    def controller_pids(self) -> list[int]:
        """Controller processes of this worker's missions (found through their private runtime directory)."""
        return scan_controllers([self.runtime]).get(str(self.runtime), [])

    def cleanup(self) -> None:
        """Stop missions, then every process of this worker (controllers, providers, the worker), whatever state they are in."""
        with contextlib.suppress(Exception):
            self.stop_missions()
        self.stop()
        for sig, wait in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
            deadline = time.monotonic() + wait
            while True:
                pids = scan_processes([self.runtime]).get(str(self.runtime), [])
                if not pids:
                    return
                for pid in pids:
                    with contextlib.suppress(OSError):
                        os.kill(pid, sig)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.3)


# --------------------------------------------------------------------------------------------
# Commands issued with unique IDs and the exactly-once check
# --------------------------------------------------------------------------------------------

@dataclass
class Issued:
    request_id: str
    worker: str
    mission_id: str | None
    operation: str
    marker: str | None  # exact prompt a task.add writes; None for reads
    accepted: bool = False
    attempts: int = 0
    last_status: int = 0
    duplicate_posts: int = 0


@dataclass
class Verification:
    issued: int = 0
    accepted: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    unfinished: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.unfinished

    def to_json(self) -> dict[str, Any]:
        return {"issued": self.issued, "accepted": self.accepted, "outcomes": dict(self.outcomes), "unfinished": self.unfinished[:20],
                "unfinished_count": len(self.unfinished), "violations": self.violations[:50], "violation_count": len(self.violations),
                "uncertain": self.uncertain[:20], "uncertain_count": len(self.uncertain), "ok": self.ok}


class Ledger:
    """Every command the harness issued, so the end state can be checked against it."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self._lock = threading.Lock()
        self._count = 0
        self.items: dict[str, Issued] = {}

    def next_id(self) -> str:
        with self._lock:
            self._count += 1
            return f"{self.prefix}-{self._count}"

    def add_task_request(self, worker: str, mission_id: str) -> tuple[Issued, dict[str, Any]]:
        request_id = self.next_id()
        marker = f"marker:{request_id}"
        issued = Issued(request_id, worker, mission_id, "task.add", marker)
        with self._lock:
            self.items[request_id] = issued
        request = {"schema_version": 1, "operation": "task.add", "params": {"prompt": marker}, "mission_id": mission_id, "target": worker,
                   "request_id": request_id}
        return issued, request

    def read_request(self, worker: str, mission_id: str | None, operation: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """A read request with a fresh ID; reads are not tracked, since a repeated read has no effect to check."""
        return {"schema_version": 1, "operation": operation, "params": params or {}, "mission_id": mission_id, "target": worker,
                "request_id": self.next_id()}

    def mutating(self) -> list[Issued]:
        with self._lock:
            return [item for item in self.items.values() if item.marker is not None]


def submit_until_accepted(api: ApiClient, issued: Issued, request: dict[str, Any], *, wait_ms: int = 0, deadline: float, stop: threading.Event | None = None,
                          pause: float = 0.25) -> tuple[int, dict[str, Any]]:
    """POST the same request ID until the coordinator accepts it (2xx) or ``deadline`` passes.

    Retrying under the same ID is safe by design: the coordinator answers a repeat with the existing
    journal entry, so a lost response never turns into a second command.
    """
    status: int = 0
    body: dict[str, Any] = {}
    while time.monotonic() < deadline and not (stop is not None and stop.is_set()):
        issued.attempts += 1
        status, body = api.timed_submit(issued.worker, request, wait_ms=wait_ms if issued.attempts == 1 else 0)
        issued.last_status = status
        if status in (200, 202):
            issued.accepted = True
            return status, body
        if status in (400, 404, 409, 422, 429):  # a definite refusal: the command was not queued
            return status, body
        time.sleep(pause)
    return status, body


def verify_exactly_once(ledger: Ledger, api: ApiClient, nodes: dict[str, WorkerNode], *, settle_seconds: float = 90.0,
                        expect_unfinished: bool = False, audit: Counter[str] | None = None) -> Verification:
    """Wait for every command to settle, then compare coordinator journal, worker command table and task markers.

    Violations (each one fails a run): a journal entry reported ``applied`` whose effect is missing
    from the worker database (silent loss); an effect present more than once (duplicate); an effect
    present for a command the journal reports as ``rejected`` or ``failed`` (phantom); a worker
    ``commands`` row count other than one for an applied command; with ``audit`` (the coordinator's
    ``command.submit`` audit counts), an accepted command that was not audited exactly once. Commands still not ``done`` after
    ``settle_seconds`` are listed as ``unfinished``. ``uncertain`` outcomes are legitimate and are
    only reported; their effect may be present at most once.
    """
    report = Verification()
    items = list(ledger.mutating())
    report.issued = len(items)
    deadline = time.monotonic() + settle_seconds
    entries: dict[str, dict[str, Any]] = {}
    pending = [item for item in items if item.accepted]
    while pending:  # at least one pass, so a zero settle time still looks at every command once
        still = []
        for item in pending:
            status, entry = api.get_command(item.request_id)
            if status == 200 and entry.get("state") == "done":
                entries[item.request_id] = entry
            else:
                still.append(item)
        pending = still
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    for item in items:
        report.accepted += int(item.accepted)
        if audit is not None and item.accepted and audit.get(item.request_id, 0) != 1:
            report.violations.append(f"{item.request_id}: {audit.get(item.request_id, 0)} submit audit entries, expected 1")
        node = nodes[item.worker]
        count = node.marker_count(item.marker or "")
        journal = entries.get(item.request_id)
        if journal is None:
            # Not done: either never accepted (a transport failure before any journal entry) or still in flight.
            status, current = api.get_command(item.request_id)
            if item.accepted or status == 200:
                if status == 404:
                    report.violations.append(f"{item.request_id}: accepted but missing from the journal (lost)")
                else:
                    report.unfinished.append(f"{item.request_id}: state {current.get('state')}")
            if count > 1:
                report.violations.append(f"{item.request_id}: effect present {count} times while unfinished (duplicate)")
            continue
        result = journal.get("result") or {}
        outcome = str(result.get("outcome", "?"))
        report.outcomes[outcome] += 1
        if outcome == "applied":
            if count == 0:
                report.violations.append(f"{item.request_id}: reported applied but the task is missing (silent loss)")
            elif count > 1:
                report.violations.append(f"{item.request_id}: applied {count} times (duplicate)")
            rows = node.command_status(item.request_id)
            if len(rows) != 1:
                report.violations.append(f"{item.request_id}: {len(rows)} command rows on the worker, expected 1")
        elif outcome == "uncertain":
            report.uncertain.append(item.request_id)
            if count > 1:
                report.violations.append(f"{item.request_id}: uncertain command applied {count} times (duplicate)")
        elif count:
            report.violations.append(f"{item.request_id}: outcome {outcome} but the task exists {count} times (phantom)")
    if expect_unfinished:
        report.unfinished.clear()
    return report


# --------------------------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------------------------

class Sampler:
    """Collects one resource sample per interval until stopped."""

    def __init__(self, coordinator: Coordinator, nodes: Callable[[], Iterable[WorkerNode]], interval: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.coordinator = coordinator
        self.nodes = nodes
        self.interval = interval
        self.clock = clock
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="sampler")
        self._origin = clock()

    def start(self) -> None:
        self._origin = self.clock()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(self.interval + 10)

    def take(self) -> dict[str, Any]:
        elapsed = self.clock() - self._origin
        coordinator = self.coordinator.stats() or {}
        workers: dict[str, Any] = {"rss_kib": 0, "fds": 0, "threads": 0, "running": 0, "max_rss_kib": 0, "by_pid": {}}
        controllers: dict[str, Any] = {"rss_kib": 0, "fds": 0, "threads": 0, "running": 0, "max_rss_kib": 0, "by_pid": {}}
        tasks: Counter[str] = Counter()
        nodes = list(self.nodes())
        by_runtime = scan_controllers([node.runtime for node in nodes])
        for node in nodes:
            stats = proc_stats(node.pid)
            if stats:
                workers["running"] += 1
                for key in ("rss_kib", "fds", "threads"):
                    workers[key] += stats[key]
                workers["max_rss_kib"] = max(workers["max_rss_kib"], stats["rss_kib"])
                workers["by_pid"][str(node.pid)] = stats["rss_kib"]
            for pid in by_runtime.get(str(node.runtime), []):
                stats = proc_stats(pid)
                if stats:
                    controllers["running"] += 1
                    for key in ("rss_kib", "fds", "threads"):
                        controllers[key] += stats[key]
                    controllers["max_rss_kib"] = max(controllers["max_rss_kib"], stats["rss_kib"])
                    controllers["by_pid"][str(pid)] = stats["rss_kib"]
            tasks.update(node.task_statuses())
        diagnostics = {}
        with contextlib.suppress(Exception):
            diagnostics = ApiClient(self.coordinator.port, self.coordinator.ca_file, self.coordinator.api_token).diagnostics()
        sample = {
            "t": round(elapsed, 2), "wall": time.strftime("%H:%M:%S"), "coordinator": coordinator, "db_bytes": self.coordinator.db_bytes(),
            "rows": self.coordinator.row_counts(), "workers": workers, "controllers": controllers, "tasks": dict(tasks),
            "connected_workers": diagnostics.get("connected_workers"), "queue_depths": diagnostics.get("command_queue_depths"),
        }
        self.samples.append(sample)
        return sample

    def _run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                self.take()
            if self._stop.wait(self.interval):
                break

    def series(self, metric: str) -> list[tuple[float, float]]:
        """``(seconds, value)`` for one named metric (the names of ``DEFAULT_BOUNDS``)."""
        points = []
        for sample in self.samples:
            value = metric_value(sample, metric)
            if value is not None:
                points.append((float(sample["t"]), float(value)))
        return points


def metric_value(sample: dict[str, Any], metric: str) -> float | None:
    coordinator = sample.get("coordinator") or {}
    workers = sample.get("workers") or {}
    controllers = sample.get("controllers") or {}
    table = {
        "coordinator_rss_kib": coordinator.get("rss_kib"), "coordinator_fds": coordinator.get("fds"), "coordinator_threads": coordinator.get("threads"),
        "coordinator_db_bytes": sample.get("db_bytes"), "workers_rss_kib": workers.get("rss_kib"), "workers_fds": workers.get("fds"),
        "workers_threads": workers.get("threads"),
        "controllers_rss_kib": controllers.get("rss_kib"), "controllers_fds": controllers.get("fds"),
        "controllers_max_rss_kib": controllers.get("max_rss_kib"), "workers_max_rss_kib": workers.get("max_rss_kib"),
    }
    value = table.get(metric)
    return None if value is None else float(value)


def evaluate_resources(samples: list[dict[str, Any]], bounds: dict[str, SlopeBound], *, warmup_fraction: float = 0.25, min_samples: int = 4,
                       warmup_seconds: float | None = None, ceilings: dict[str, tuple[float, str]] | None = None,
                       process_bounds: dict[str, SlopeBound] | None = None) -> list[SlopeResult]:
    """Slope and ceiling checks. ``process_bounds`` holds the unscaled bounds of ``controllers_rss_kib`` and ``workers_rss_kib``: when
    the samples carry the per-process sizes (``by_pid``), that metric is judged per process (``check_per_process_slope``), not on the sum."""
    results = []
    rows = [(float(s["t"]), float(sum((s.get("rows") or {}).values()))) for s in samples]

    def rows_at(moment: float) -> float:
        return min(rows, key=lambda point: abs(point[0] - moment))[1] if rows else 0.0

    def database_growth_explained_by_rows(first: float, last: float) -> float:
        return max(0.0, rows_at(last) - rows_at(first)) * DB_BYTES_PER_ROW

    def coordinator_memory_explained_by_rows(first: float, last: float) -> float:
        return max(0.0, rows_at(last) - rows_at(first)) * COORDINATOR_RSS_BYTES_PER_ROW / 1024.0

    by_pid: dict[str, dict[str, list[tuple[float, float]]]] = {"controllers_rss_kib": {}, "workers_rss_kib": {}}
    for metric, table in by_pid.items():
        for sample in samples:
            for pid, kib in ((sample.get(metric.split("_")[0]) or {}).get("by_pid") or {}).items():
                table.setdefault(str(pid), []).append((float(sample["t"]), float(kib)))

    for metric, bound in bounds.items():
        if by_pid.get(metric) and process_bounds and metric in process_bounds:
            results.append(check_per_process_slope(metric, by_pid[metric], process_bounds[metric],
                                                   warmup_seconds=warmup_seconds if warmup_seconds is not None else 0.0, min_samples=min_samples))
            continue
        points = [(float(s["t"]), float(v)) for s in samples if (v := metric_value(s, metric)) is not None]
        explained = (database_growth_explained_by_rows if metric == "coordinator_db_bytes"
                     else coordinator_memory_explained_by_rows if metric == "coordinator_rss_kib" else None)
        results.append(check_slope(metric, points, bound, warmup_fraction=warmup_fraction, min_samples=min_samples, explained=explained,
                                   warmup_seconds=warmup_seconds))
    for metric, (ceiling, unit) in (ceilings if ceilings is not None else DEFAULT_CEILINGS).items():
        points = [(float(s["t"]), float(v)) for s in samples if (v := metric_value(s, metric)) is not None]
        results.append(check_ceiling(metric, points, ceiling, unit))
    return results


def results_to_json(results: Sequence[SlopeResult]) -> list[dict[str, Any]]:
    return [asdict(result) for result in results]


def wait_until(predicate: Callable[[], Any], timeout: float, interval: float = 0.2, message: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {message}")
        time.sleep(interval)


def seeded(seed: int, *parts: object) -> random.Random:
    return random.Random(f"{seed}:" + ":".join(str(part) for part in parts))
