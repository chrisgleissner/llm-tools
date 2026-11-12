#!/usr/bin/env python3
"""Soak test for the distributed Bridge (PRD-007).

One real ``bridge-coordinator``, N real worker processes and M missions driven by the fake provider
``scripts/bridge_soak_provider.py`` (output, questions, rate limits, crashes) run for ``--duration``
seconds while the harness issues commands with unique IDs and samples the coordinator.

Pass criteria (each is a line of the report, and any failure gives exit status 1):

* no lost or duplicated command: every accepted command is ``done`` in the coordinator journal, every
  ``applied`` effect exists exactly once in the worker database, and no effect exists for a command
  that did not apply (``dist_harness.verify_exactly_once``);
* no unbounded growth of coordinator memory, file descriptors, threads or database size, and of the
  workers' memory, descriptors and threads: the least-squares slope after warm-up (``--warmup-seconds``,
  20 minutes by default) is compared with ``dist_harness.DEFAULT_BOUNDS``, the controllers one process at a
  time; the database must also be explained by its row count; no single controller, worker or coordinator
  may pass the memory ceilings of ``dist_harness.DEFAULT_CEILINGS``;
* p99 API latency inside the budgets of ``dist_harness.DEFAULT_LATENCY_BUDGET_MS``;
* the coordinator process survived without a panic, and every worker is online at the end.

Defaults give the PRD's scaled-down run (30 minutes, 10 workers, 50 missions). The full 24-hour run
is ``--duration 86400``. Nothing real is touched: see the safety rules in ``dist_harness``.

    python scripts/bridge_soak.py --duration 300 --workers 3 --missions 10 --sample-seconds 15
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dist_harness as h  # noqa: E402

EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_BINARY = 3
MIN_SAMPLES_AFTER_WARMUP = 4
WARMUP_SECONDS = 1200.0  # memory of controllers, workers and the coordinator is settled 20 minutes after the start (soak-and-chaos.md)
WARMUP_MAX_SHARE = 2 / 3  # a short run cannot ignore more than two thirds of itself

# Items that need features which are not on this branch (PR #31); each names what it expects.
PHASE_B: list[dict[str, str]] = [
    {"id": "coordinator-task-count", "todo": "TODO(phase B): sample the coordinator's own task count. Until then the thread count of the process is the "
     "task metric (coordinator_threads). Expected: a `tasks` integer in GET /api/v1/diagnostics; add it to Sampler.take and DEFAULT_BOUNDS."},
]


def effective_warmup(duration: float, fraction: float, seconds: float) -> float:
    """Seconds of the run that the slope fit ignores: ``seconds`` capped at two thirds of the run, and at least ``fraction`` of it."""
    return max(duration * fraction, min(seconds, duration * WARMUP_MAX_SHARE))


COUNT_METRIC_SUFFIXES = ("_fds", "_threads")


def chosen_bounds(args: argparse.Namespace) -> dict[str, h.SlopeBound]:
    """The slope bounds of this run: the per-hour numbers from the command line, and the floors of the descriptor and thread metrics
    multiplied by ``--count-floor-multiplier``."""
    chosen: dict[str, h.SlopeBound] = {}
    for metric, bound in h.DEFAULT_BOUNDS.items():
        floor = bound.floor * (args.count_floor_multiplier if metric.endswith(COUNT_METRIC_SUFFIXES) else 1.0)
        chosen[metric] = h.SlopeBound(getattr(args, f"bound_{metric}"), floor, bound.unit, bound.per_process)
    return chosen


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coordinator-bin", help="bridge-coordinator binary (default: $BRIDGE_COORDINATOR_BIN, then coordinator/target/{release,debug})")
    parser.add_argument("--build", action="store_true", help="run cargo build --release when no binary is found")
    parser.add_argument("--duration", type=float, default=1800.0, help="seconds of load (default: 1800, the scaled-down run; 86400 for the full 24 hours)")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--missions", type=int, default=50, help="missions in total, spread over the workers")
    parser.add_argument("--sample-seconds", type=float, default=60.0, help="seconds between resource samples (default: 60)")
    parser.add_argument("--command-rate", type=float, default=2.0, help="commands per second issued to the fleet (default: 2)")
    parser.add_argument("--backlog", type=int, default=30, help="tasks queued per mission at the start")
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--mix", default="80,5,10,5", help="fake provider weights output,question,ratelimit,crash (default: 80,5,10,5)")
    parser.add_argument("--provider-speed", type=float, default=0.1, help="multiplier for the fake provider delays; 0.1 makes one task take about 16 s (default: 0.1)")
    parser.add_argument("--limit-every", type=float, default=240.0, help="seconds between injected capacity limits per worker; 0 disables (default: 240)")
    parser.add_argument("--limit-seconds", type=float, default=20.0, help="how long an injected capacity limit lasts (default: 20)")
    parser.add_argument("--warmup-fraction", type=float, default=0.25, help="smallest share of the run ignored by the slope fit (default: 0.25)")
    parser.add_argument("--warmup-seconds", type=float, default=WARMUP_SECONDS,
                        help=f"seconds ignored by the slope fit, at most two thirds of the run (default: {WARMUP_SECONDS:g}, the time memory takes to settle; see soak-and-chaos.md)")
    parser.add_argument("--settle-seconds", type=float, default=120.0, help="time allowed for outstanding commands to finish after the load stops")
    parser.add_argument("--p99-read-ms", type=float, default=h.DEFAULT_LATENCY_BUDGET_MS["read"])
    parser.add_argument("--p99-submit-ms", type=float, default=h.DEFAULT_LATENCY_BUDGET_MS["submit"])
    parser.add_argument("--p99-roundtrip-ms", type=float, default=h.DEFAULT_LATENCY_BUDGET_MS["roundtrip"])
    for metric, bound in h.DEFAULT_BOUNDS.items():
        parser.add_argument(f"--bound-{metric.replace('_', '-')}", type=float, default=bound.per_hour, metavar="PER_HOUR",
                            help=f"largest slope of {metric} in {bound.unit} per hour{' and per process' if bound.per_process else ''} (default: {bound.per_hour:g})")
    parser.add_argument("--count-floor-multiplier", type=float, default=1.0, metavar="FACTOR",
                        help="multiply the floors of the descriptor and thread metrics by this factor (default: 1). A run of a minute or less cannot "
                             "settle the coordinator's thread pool, which grows while database calls overlap, so a short run needs a floor above that growth")
    parser.add_argument("--report", type=Path, help="write the JSON report here (default: soak-report.json in the run directory)")
    parser.add_argument("--keep", action="store_true", help="keep the run directory (always kept on failure)")
    parser.add_argument("--run-dir", type=Path, help="use this empty directory instead of a new temporary one")
    return parser.parse_args(argv)


def validate(args: argparse.Namespace) -> str | None:
    if args.workers < 1 or args.missions < args.workers:
        return "--missions must be at least --workers (every worker needs a mission) and --workers at least 1"
    if args.sample_seconds <= 0 or args.duration <= 0:
        return "--duration and --sample-seconds must be positive"
    if args.count_floor_multiplier < 1.0:
        return "--count-floor-multiplier must be at least 1"
    usable = (args.duration - effective_warmup(args.duration, args.warmup_fraction, args.warmup_seconds)) / args.sample_seconds
    if usable < MIN_SAMPLES_AFTER_WARMUP:
        return (f"{args.duration:g}s with a sample every {args.sample_seconds:g}s leaves {usable:.1f} samples after warm-up; "
                f"the slope check needs {MIN_SAMPLES_AFTER_WARMUP}. Lower --sample-seconds or raise --duration")
    try:
        h.ProviderMix(*[float(part) for part in args.mix.split(",")])
    except (TypeError, ValueError):
        return "--mix needs four comma-separated numbers"
    return None


class Soak:
    def __init__(self, args: argparse.Namespace, binary: Path, base: Path) -> None:
        self.args = args
        self.base = base
        self.rng = random.Random(args.seed)
        self.coordinator = h.Coordinator(binary, base / "coordinator")
        weights = [float(part) for part in args.mix.split(",")]
        self.nodes: dict[str, h.WorkerNode] = {}
        for index in range(args.workers):
            name = f"soak-w{index + 1}"
            self.nodes[name] = h.WorkerNode(name, base / name, seed=args.seed + index, mix=h.ProviderMix(*weights), speed=args.provider_speed)
        self.ledger = h.Ledger(f"soak{args.seed % 100000}")
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.sampler = h.Sampler(self.coordinator, lambda: list(self.nodes.values()), args.sample_seconds)
        self.events: list[str] = []
        self.restarted_missions = 0
        self.mission_nodes: list[tuple[str, str]] = []  # (worker name, mission id)

    def note(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        self.events.append(line)
        print(line, flush=True)

    # ---- setup
    def setup(self) -> None:
        self.note(f"coordinator {self.coordinator.binary} on {h.HOST}")
        self.coordinator.setup()
        for node in self.nodes.values():
            node.enroll(self.coordinator)
            node.start()
        h.wait_until(lambda: sum(1 for item in self.coordinator.api.workers() if item.get("state") == "online") == len(self.nodes), 60,
                     message="all workers online")
        self.note(f"{len(self.nodes)} workers online")
        names = list(self.nodes)
        jobs = [(names[index % len(names)], index) for index in range(self.args.missions)]

        def create(job: tuple[str, int]) -> tuple[str, str]:
            name, index = job
            node = self.nodes[name]
            mission_id = node.create_mission(f"m{index + 1}")
            return name, mission_id

        with ThreadPoolExecutor(4) as pool:
            self.mission_nodes = list(pool.map(create, jobs))
        self.queue_backlog()
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda item: self.nodes[item[0]].start_mission(item[1]), self.mission_nodes))
        self.note(f"{len(self.mission_nodes)} missions started with {self.args.backlog} queued tasks each")

    def queue_backlog(self) -> None:
        from llm_tools.bridge_store import BridgeStore

        for name, node in self.nodes.items():
            with BridgeStore(node.db_path) as store:
                for mission_id in node.missions:
                    with store._transaction():
                        for index in range(self.args.backlog):
                            store.add_task(mission_id, f"backlog task {index + 1}")

    # ---- background activities
    def spawn(self, target: Any, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)

    def command_driver(self) -> None:
        """Issue commands at ``--command-rate`` through a few worker threads; retries keep the same ID until accepted."""
        interval = 1.0 / self.args.command_rate
        api = self.coordinator.api
        rng = random.Random(self.args.seed + 1)
        recent: list[tuple[h.Issued, dict[str, Any]]] = []
        while not self.stop.wait(interval):
            name, mission_id = rng.choice(self.mission_nodes)
            roll = rng.random()
            if roll < 0.6:
                issued, request = self.ledger.add_task_request(name, mission_id)
                h.submit_until_accepted(api, issued, request, deadline=time.monotonic() + 30, stop=self.stop)
                recent.append((issued, request))
                del recent[:-50]
            elif roll < 0.7 and recent:
                issued, request = rng.choice(recent)  # a client retry after a lost response: must not run twice
                status, _ = api.timed_submit(issued.worker, request)
                issued.duplicate_posts += 1
            else:
                operation = rng.choice(["mission.status", "task.list", "mission.list"])
                request = self.ledger.read_request(name, None if operation == "mission.list" else mission_id, operation)
                api.timed_submit(name, request, wait_ms=15000)

    def read_probe(self) -> None:
        api = self.coordinator.api
        paths = ["/api/v1/missions?limit=50", "/api/v1/workers", "/api/v1/diagnostics", "/api/v1/missions?status=RUNNING&limit=20"]
        index = 0
        while not self.stop.wait(1.0):
            api.request("GET", paths[index % len(paths)], label="read")
            index += 1

    def usage_flipper(self) -> None:
        """Every ``--limit-every`` seconds each worker's capacity runs out for ``--limit-seconds`` (staggered)."""
        rng = random.Random(self.args.seed + 2)
        next_at = {name: time.monotonic() + rng.uniform(0.2, 1.0) * self.args.limit_every for name in self.nodes}
        limited_until: dict[str, float] = {}
        while not self.stop.wait(2.0):
            now = time.monotonic()
            for name, node in self.nodes.items():
                if name in limited_until and now >= limited_until[name]:
                    node.set_usage(h.usage_payload())
                    del limited_until[name]
                    next_at[name] = now + self.args.limit_every * rng.uniform(0.7, 1.3)
                elif name not in limited_until and now >= next_at[name]:
                    node.set_usage(h.usage_payload(limited_for=self.args.limit_seconds))
                    limited_until[name] = now + self.args.limit_seconds
                    self.note(f"{name}: capacity limited for {self.args.limit_seconds:g}s")

    def operator(self) -> None:
        """Restart a mission whose controller ended (all tasks done or aborted) while tasks are still queued."""
        while not self.stop.wait(15.0):
            for name, mission_id in self.mission_nodes:
                node = self.nodes[name]
                try:
                    with node.database() as connection:
                        row = connection.execute("SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
                        queued = connection.execute("SELECT COUNT(*) FROM tasks WHERE mission_id=? AND status IN ('QUEUED','READY')", (mission_id,)).fetchone()[0]
                except Exception:  # noqa: BLE001 - a busy database is retried at the next pass
                    continue
                if row and row[0] in {"COMPLETED", "FAILED", "STOPPED", "PAUSED", "BLOCKED"} and queued:
                    result = node.cli("start", "--mission", mission_id, check=False, timeout=30)
                    if result.returncode == 0:
                        self.restarted_missions += 1

    # ---- run
    def run(self) -> dict[str, Any]:
        self.setup()
        self.sampler.start()
        for target, name in ((self.command_driver, "commands-a"), (self.command_driver, "commands-b"), (self.read_probe, "reads"),
                             (self.usage_flipper, "limits"), (self.operator, "operator")):
            self.spawn(target, name)
        started = time.monotonic()
        next_report = started + 60
        while time.monotonic() - started < self.args.duration:
            time.sleep(min(1.0, max(0.0, self.args.duration - (time.monotonic() - started))))
            if time.monotonic() >= next_report:
                next_report += 60
                last = self.sampler.samples[-1] if self.sampler.samples else {}
                coordinator = last.get("coordinator") or {}
                self.note(f"t={int(time.monotonic() - started)}s rss={coordinator.get('rss_kib')}KiB fds={coordinator.get('fds')} "
                          f"db={last.get('db_bytes')} rows={last.get('rows')} commands={len(self.ledger.items)}")
            if self.coordinator.pid is None:
                self.note("the coordinator process died during the run")
                break
        self.stop.set()
        for thread in self.threads:
            thread.join(60)
        self.sampler.take()
        self.sampler.stop()
        self.note("load stopped; waiting for outstanding commands")
        return self.evaluate()

    def evaluate(self) -> dict[str, Any]:
        args = self.args
        verification = h.verify_exactly_once(self.ledger, self.coordinator.api, self.nodes, settle_seconds=args.settle_seconds,
                                             audit=self.coordinator.audit_submissions())
        chosen = chosen_bounds(args)
        bounds = h.scale_bounds(chosen, workers=len(self.nodes), controllers=len(self.mission_nodes))
        warmup = effective_warmup(args.duration, args.warmup_fraction, args.warmup_seconds)
        slopes = h.evaluate_resources(self.sampler.samples, bounds, warmup_fraction=args.warmup_fraction, min_samples=MIN_SAMPLES_AFTER_WARMUP,
                                      warmup_seconds=warmup, process_bounds=chosen)
        explained = h.check_database_explained_by_rows(self.sampler.samples)
        events_cap = len(self.nodes) * h.MAX_EVENTS_PER_WORKER
        max_events = max((int((s.get("rows") or {}).get("events", 0)) for s in self.sampler.samples), default=0)
        backlog = h.check_command_backlog(self.sampler.samples)
        budgets = {"read": args.p99_read_ms, "submit": args.p99_submit_ms, "roundtrip": args.p99_roundtrip_ms}
        latency = h.check_latency(self.coordinator.recorder, budgets)
        workers_online = sum(1 for item in self.coordinator.api.workers() if item.get("state") == "online")
        tasks: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        missions: dict[str, int] = {}
        for node in self.nodes.values():
            for key, value in node.task_statuses().items():
                tasks[key] = tasks.get(key, 0) + value
            for key, value in node.operation_outcomes().items():
                outcomes[key] = outcomes.get(key, 0) + value
            for key, value in node.mission_statuses().items():
                missions[key] = missions.get(key, 0) + value
        checks = [
            {"check": "commands exactly once", "verdict": "pass" if verification.ok else "fail",
             "detail": f"{verification.issued} issued, {verification.accepted} accepted, outcomes {dict(verification.outcomes)}, "
                       f"{len(verification.violations)} violations, {len(verification.unfinished)} unfinished, {len(verification.uncertain)} uncertain"},
            *[{"check": item.metric if item.metric.endswith(" ceiling") else f"slope {item.metric}", "verdict": item.verdict, "detail": item.detail} for item in slopes],
            {"check": "database explained by rows", "verdict": "fail" if explained else "pass", "detail": "; ".join(explained[:3]) or "ok"},
            {"check": "worker command backlog", "verdict": "fail" if backlog else "pass",
             "detail": "; ".join(backlog[:3]) or f"no worker above {h.MAX_WORKER_BACKLOG} unfinished commands"},
            {"check": "events within retention cap", "verdict": "fail" if max_events > events_cap else "pass", "detail": f"max {max_events}, cap {events_cap}"},
            *[{"check": f"p99 latency {item['class']}", "verdict": item["verdict"],
               "detail": f"p99 {item['p99_ms']} ms (budget {item['budget_p99_ms']}), n={item['count']}, errors={item['errors']}"} for item in latency],
            {"check": "coordinator survived", "verdict": "pass" if self.coordinator.pid and not self.coordinator.log_has_panic() else "fail",
             "detail": "running, no panic in log" if self.coordinator.pid else self.coordinator.log_tail(5)},
            {"check": "workers online at end", "verdict": "pass" if workers_online == len(self.nodes) else "fail", "detail": f"{workers_online}/{len(self.nodes)}"},
            {"check": "missions made progress", "verdict": "pass" if tasks.get("COMPLETED", 0) > 0 else "fail", "detail": f"task statuses {tasks}"},
        ]
        return {
            "tool": "bridge_soak", "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "config": {key: (str(value) if isinstance(value, Path) else value) for key, value in vars(args).items()},
            "checks": checks, "passed": all(item["verdict"] == "pass" for item in checks), "phase_b": PHASE_B,
            "commands": verification.to_json(), "resource_slopes": h.results_to_json(slopes), "latency": latency,
            "workload": {"task_statuses": tasks, "operation_exit_codes": outcomes, "mission_statuses": missions,
                         "missions_restarted_by_operator": self.restarted_missions},
            "samples": self.sampler.samples, "events": self.events,
        }

    def cleanup(self) -> None:
        self.stop.set()
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda node: node.cleanup(), self.nodes.values()))
        self.coordinator.stop()
        for node in self.nodes.values():
            shutil.rmtree(node.runtime, ignore_errors=True)


def render_summary(report: dict[str, Any]) -> str:
    lines = ["", "Soak summary", "============"]
    for item in report["checks"]:
        lines.append(f"  {item['verdict'].upper():12} {item['check']}: {item['detail']}")
    lines.append(f"Workload: {report['workload']}")
    for entry in report.get("phase_b", []):
        lines.append(f"  TODO         {entry['id']}: {entry['todo']}")
    lines.append("RESULT: " + ("PASS" if report["passed"] else "FAIL"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    problem = validate(args)
    if problem:
        print(f"bridge_soak: {problem}", file=sys.stderr)
        return EXIT_USAGE
    binary = h.find_coordinator_binary(args.coordinator_bin, build=args.build)
    if binary is None:
        print("bridge_soak: no bridge-coordinator binary; build it (cargo build --release --manifest-path coordinator/Cargo.toml), pass --coordinator-bin or --build", file=sys.stderr)
        return EXIT_NO_BINARY
    if args.run_dir:
        base = args.run_dir
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = h.short_temp_dir("soak-")
    soak = Soak(args, binary, base)
    report: dict[str, Any] = {}
    try:
        report = soak.run()
    except BaseException as exc:  # noqa: BLE001 - report the failure and still clean up
        report = {"tool": "bridge_soak", "passed": False, "checks": [{"check": "run", "verdict": "fail", "detail": f"{type(exc).__name__}: {exc}"}],
                  "workload": {}, "events": soak.events, "samples": soak.sampler.samples}
        print(f"bridge_soak: run aborted: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        soak.cleanup()
    path = args.report or base / "soak-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, default=str))
    print(render_summary(report))
    print(f"report: {path}")
    if report.get("passed") and not args.keep and not args.run_dir:
        shutil.rmtree(base, ignore_errors=True)
        if args.report is None:
            print("(run directory removed; pass --keep to keep it)")
    else:
        print(f"run directory kept: {base}")
    return 0 if report.get("passed") else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
