#!/usr/bin/env python3
"""Measure Bridge database growth per provider run and project a year of continuous use.

The script never launches a provider and never touches real Bridge state: it builds a throwaway
database, writes the rows one provider run produces (through the store's own methods, with event
payloads of the sizes measured from the fake-provider end-to-end tests), and reports the bytes
per run, per command and per thinned capacity refresh, including SQLite page and index overhead.

The projection assumes one mission running continuously with the default retention settings. The
mission is not finished, so event pruning applies only to the controller's timer events (heartbeats
and capacity refreshes); every task, operation and verification event is kept.

    python scripts/bridge_retention_budget.py [--runs-per-day 144] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_tools.bridge_store import BridgeStore  # noqa: E402

# Payload sizes (bytes of data_json) measured from the events a fake-provider run writes
# (tests/test_bridge_quota_recovery_e2e.py, one operation each).
RUN_EVENT_PAYLOADS = {
    "provider-selection": 970,
    "prompt-assembled": 193,
    "repository-checkpoint": 160,
    "operation-progress": 260,
    "provider-result-diagnosed": 620,
    "native-session-restoration": 97,
}
CAPACITY_REFRESH_PAYLOAD = 1536  # measured: refresh_capacity_observation() with two providers (claude, codex) of two reset windows each
CAPACITY_REFRESH_THINNED_PER_DAY = 144  # one per 10 minutes once older than an hour
PROMPT_BYTES = 600
GIB = 1024 ** 3


def database_bytes(store: BridgeStore) -> int:
    store.db.commit() if store.db.in_transaction else None
    pages = store.db.execute("PRAGMA page_count").fetchone()[0]
    free = store.db.execute("PRAGMA freelist_count").fetchone()[0]
    size = store.db.execute("PRAGMA page_size").fetchone()[0]
    return int((pages - free) * size)


def padded(size: int) -> dict[str, str]:
    base = len(json.dumps({"p": ""}))
    return {"p": "x" * max(0, size - base)}


def measure(runs: int) -> dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="bridge-retention-budget-") as directory:
        root = Path(directory)
        with BridgeStore(root / "bridge.sqlite3") as store:
            mission = store.create_mission("budget", "objective", cwd=root)["id"]
            store.set_mission_status(mission, "RUNNING")
            baseline = database_bytes(store)
            for index in range(runs):
                task = store.add_task(mission, f"task {index} " + "p" * PROMPT_BYTES)
                store.set_task_status(task, "READY")
                operation = store.begin_operation(task, iteration=1, station="headless")
                store.mark_operation_running(operation, route_id="codex", model="gpt")
                for kind, size in RUN_EVENT_PAYLOADS.items():
                    store.record_runtime_event(mission, kind, padded(size))
                store.record_operation_process(operation, process_pid=1000 + index, process_start="12345")
                store.finish_operation(operation, exit_code=0, logs_path=None)
            per_run = (database_bytes(store) - baseline) / runs
            events_per_run = store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] / runs
            before = database_bytes(store)
            for index in range(runs):
                store.record_runtime_event(mission, "capacity-refresh", padded(CAPACITY_REFRESH_PAYLOAD))
            per_refresh = (database_bytes(store) - before) / runs
            before = database_bytes(store)
            for index in range(runs):
                command = store.enqueue_command(mission, {"action": "task.add", "prompt": "p" * PROMPT_BYTES})
                store.finish_command(command["id"], status="APPLIED", result={"task_id": index})
            per_command = (database_bytes(store) - before) / runs
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"bytes_per_run": per_run, "events_per_run": events_per_run, "bytes_per_capacity_refresh": per_refresh, "bytes_per_command": per_command}


def project(measured: dict[str, float], runs_per_day: float) -> dict[str, float]:
    runs = runs_per_day * 365
    run_bytes = runs * measured["bytes_per_run"]
    refresh_bytes = CAPACITY_REFRESH_THINNED_PER_DAY * 365 * measured["bytes_per_capacity_refresh"]
    # One task per run, added by one command; commands are deleted after 30 days.
    command_bytes = runs_per_day * 30 * measured["bytes_per_command"]
    # Heartbeats: the last hour at one per 5 s.
    heartbeat_bytes = 720 * 280
    # Provider run directories: the latest 20 are kept at up to (1 + 3 rotations) x 4 MiB per output file.
    log_worst = 20 * 4 * 4 * 1024 * 1024
    return {
        "runs_per_year": runs,
        "database_run_rows": run_bytes,
        "database_thinned_capacity_refreshes": refresh_bytes,
        "database_commands": command_bytes,
        "database_heartbeats": heartbeat_bytes,
        "database_total": run_bytes + refresh_bytes + command_bytes + heartbeat_bytes,
        "provider_logs_worst_case": log_worst,
        "state_total_worst_case": run_bytes + refresh_bytes + command_bytes + heartbeat_bytes + log_worst,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=2000, help="provider runs to write for the measurement")
    parser.add_argument("--runs-per-day", type=float, default=144.0, help="provider runs per day for the projection (default: one every 10 minutes, around the clock)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not 100 <= args.runs <= 20000:
        parser.error("--runs must be between 100 and 20000")
    if sqlite3.sqlite_version_info < (3, 25):
        parser.error("SQLite 3.25 or newer is required")
    measured = measure(args.runs)
    projected = project(measured, args.runs_per_day)
    if args.json:
        print(json.dumps({"measured": measured, "projected": projected, "runs_per_day": args.runs_per_day}, indent=2))
        return
    for key, value in measured.items():
        print(f"{key:34s} {value:12.1f}")
    print(f"projection at {args.runs_per_day:g} runs/day:")
    for key, value in projected.items():
        shown = f"{value:14.0f}" if key == "runs_per_year" else f"{value / 1024 / 1024:10.1f} MiB"
        print(f"  {key:36s} {shown}")
    print(f"  fraction of a 1 GiB budget          {projected['state_total_worst_case'] / GIB:10.1%}")


if __name__ == "__main__":
    main()
