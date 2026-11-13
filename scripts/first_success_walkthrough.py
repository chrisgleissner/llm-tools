#!/usr/bin/env python3
"""Timed first-success walkthrough (PRD-001): nothing to a running mission visible through the coordinator.

The script performs the steps a new user types, against throw-away directories and a fake provider:

  1. init       bridge-coordinator init (certificate, config, data directory)
  2. serve      bridge-coordinator serve, wait until /healthz answers
  3. token      create the operator API token
  4. enroll     create a worker token and run `llm-bridge worker enroll`
  5. connect    start `llm-bridge worker run`, wait until the coordinator lists the worker online
  6. mission    create and start a mission with the fake provider
  7. visible    wait until the coordinator's event feed shows the mission's events

Every step is timed. The run fails when a step fails or the total exceeds the budget
(default 300 seconds, the PRD-001 limit). All state lives under one temporary directory with its own
LLM_BRIDGE_STATE_DIR and XDG_RUNTIME_DIR; the operator's real Bridge state is never read or written.
Linux only: macOS and Windows have no scripted walkthrough yet (see docs/distributed/install.md).

    python scripts/first_success_walkthrough.py [--coordinator-bin PATH] [--budget SECONDS] [--json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dist_harness as h  # noqa: E402

DEFAULT_BUDGET_SECONDS = 300.0


class Walkthrough:
    def __init__(self, binary: Path, base: Path, budget: float) -> None:
        self.binary = binary
        self.base = base
        self.budget = budget
        self.steps: list[tuple[str, float]] = []
        self.coordinator = h.Coordinator(binary, base / "coordinator")
        self.node = h.WorkerNode("first-success", base / "worker", speed=0.0, mix=h.ProviderMix(output=1, question=0, ratelimit=0, crash=0))
        self.mission_id = ""

    def step(self, name: str, action: Callable[[], Any]) -> Any:
        started = time.monotonic()
        result = action()
        self.steps.append((name, time.monotonic() - started))
        return result

    def wait(self, what: str, predicate: Callable[[], bool], timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.2)
        raise RuntimeError(f"timed out after {timeout:g}s waiting for {what}")

    def worker_online(self) -> bool:
        return any(item.get("name") == self.node.name and item.get("state") == "online" for item in self.coordinator.api.workers())

    def mission_events(self) -> bool:
        return any(str(item.get("mission_id") or item.get("mission") or "") == self.mission_id or self.mission_id in json.dumps(item)
                   for item in self.coordinator.api.events_after(self.node.name, 0))

    def run(self) -> dict[str, Any]:
        coordinator, node = self.coordinator, self.node
        error = ""
        try:
            self.step("init", coordinator.init)
            self.step("serve", coordinator.start)
            coordinator.api_token = self.step("token", lambda: coordinator.run("token", "create", "first-success", "--expires-days", "1", "--scope", "admin"))
            self.step("enroll", lambda: node.enroll(coordinator))
            self.step("connect", lambda: (node.start(), self.wait("the worker to connect", self.worker_online, 60)))

            def mission() -> None:
                self.mission_id = node.create_mission("first-success", prompt="first success", run_provider=True)
                node.start_mission(self.mission_id)

            self.step("mission", mission)
            self.step("visible", lambda: self.wait("mission events in the coordinator", self.mission_events, 120))
        except Exception as exc:  # noqa: BLE001 - the report names the failed step
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self.cleanup()
        total = sum(seconds for _, seconds in self.steps)
        ok = not error and total <= self.budget
        if not error and not ok:
            error = f"total {total:.1f}s exceeds the {self.budget:g}s budget"
        return {"ok": ok, "total_seconds": round(total, 2), "budget_seconds": self.budget, "error": error,
                "steps": [{"step": name, "seconds": round(seconds, 2)} for name, seconds in self.steps]}

    def cleanup(self) -> None:
        for action in (self.node.stop_missions, self.node.stop, self.coordinator.stop):
            try:
                action()
            except Exception:  # noqa: BLE001 - best effort, the directory is removed next
                pass


def render(report: dict[str, Any]) -> str:
    lines = [f"{item['step']:<10}{item['seconds']:>8.2f}s" for item in report["steps"]]
    lines.append(f"{'total':<10}{report['total_seconds']:>8.2f}s (budget {report['budget_seconds']:g}s)")
    lines.append("PASS" if report["ok"] else f"FAIL: {report['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coordinator-bin", help="path of bridge-coordinator (default: BRIDGE_COORDINATOR_BIN, then coordinator/target)")
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET_SECONDS, help="maximum total seconds (default 300)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    binary = h.find_coordinator_binary(args.coordinator_bin)
    if binary is None:
        print("error: no bridge-coordinator binary found; build it with `cargo build --release` in coordinator/ or pass --coordinator-bin", file=sys.stderr)
        return 2
    base = h.short_temp_dir("lb-first-success-")
    try:
        report = Walkthrough(binary, base, args.budget).run()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
