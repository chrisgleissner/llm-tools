#!/usr/bin/env python3
"""Measure Bridge database growth per provider run and project a year of continuous use.

The script never launches a provider and never touches real Bridge state: it builds a throwaway
database, writes the rows one provider run produces (through the store's own methods, with event
payloads of the sizes measured from the fake-provider end-to-end tests), and reports the bytes
per run, per command and per thinned capacity refresh, including SQLite page and index overhead.

The projection assumes one mission that is never finished, with the default retention settings: events
older than 90 days lose their diagnostic kinds (the newest of each kind stays), capacity observations
older than 90 days are thinned to one per hour, and audit records, task rows and operation rows stay.

    python scripts/bridge_retention_budget.py [--runs-per-day 144 480] [--json]
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
            # The same rows after the retention rules for a mission that is not finished have removed every
            # diagnostic event (the newest of each kind stays).
            store.prune_retention(events_before="9999-01-01T00:00:00+00:00", chunk=5000, budget_seconds=600)
            store.db.execute("VACUUM")
            retained_per_run = (database_bytes(store) - baseline) / runs
            retained_events_per_run = store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] / runs
            before = database_bytes(store)
            for index in range(runs):
                store.record_runtime_event(mission, "capacity-refresh", padded(CAPACITY_REFRESH_PAYLOAD))
            per_refresh = (database_bytes(store) - before) / runs
            before = database_bytes(store)
            for index in range(runs):
                command = store.enqueue_command(mission, {"action": "task.add", "prompt": "p" * PROMPT_BYTES})
                store.finish_command(command["id"], status="APPLIED", result={"task_id": index})
            per_command = (database_bytes(store) - before) / runs
            before = database_bytes(store)
            for index in range(runs):
                store.record_events_pruned(mission, {"deleted": {"controller-heartbeat": 180, "capacity-refresh": 6}, "heartbeat_before": "2027-01-01T00:00:00+00:00", "capacity_before": "2027-01-01T00:00:00+00:00"})
            per_pruned_event = (database_bytes(store) - before) / runs
            store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {
        "bytes_per_run": per_run,
        "events_per_run": events_per_run,
        "bytes_per_run_after_retention": retained_per_run,
        "events_per_run_after_retention": retained_events_per_run,
        "bytes_per_capacity_refresh": per_refresh,
        "bytes_per_command": per_command,
        "bytes_per_prune_audit_event": per_pruned_event,
    }


def project(measured: dict[str, float], runs_per_day: float, *, events_days: int = 90) -> dict[str, float]:
    """One year of a mission that is never finished, with the default retention settings."""
    recent_days = events_days
    old_days = 365 - events_days
    run_recent = runs_per_day * recent_days * measured["bytes_per_run"]
    run_old = runs_per_day * old_days * measured["bytes_per_run_after_retention"]
    # Capacity observations: one per 10 minutes (INT-011) for the last events_days, one per hour before.
    refresh = (144 * recent_days + 24 * old_days) * measured["bytes_per_capacity_refresh"]
    # One task per run, added by one command; commands are deleted after 30 days.
    command_bytes = runs_per_day * 30 * measured["bytes_per_command"]
    # Heartbeats: the last hour at one per 5 s.
    heartbeat_bytes = 720 * 280
    # One events-pruned audit event per 15-minute pass (heartbeat pruning) plus at most one retention audit event per hour.
    audit = (96 + 24) * 365 * measured["bytes_per_prune_audit_event"]
    # Provider run directories: the latest 20 are kept at up to (1 + 3 rotations) x 4 MiB per output file.
    log_worst = 20 * 4 * 4 * 1024 * 1024
    database = run_recent + run_old + refresh + command_bytes + heartbeat_bytes + audit
    return {
        "runs_per_year": runs_per_day * 365,
        "database_run_rows_last_90_days": run_recent,
        "database_run_rows_older": run_old,
        "database_capacity_refreshes": refresh,
        "database_commands": command_bytes,
        "database_heartbeats": heartbeat_bytes,
        "database_pruning_audit_events": audit,
        "database_total": database,
        "provider_logs_worst_case": log_worst,
        "state_total_worst_case": database + log_worst,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=2000, help="provider runs to write for the measurement")
    parser.add_argument("--runs-per-day", type=float, nargs="+", default=[144.0, 480.0], help="provider runs per day to project (default: one every 10 minutes and one every 3 minutes, around the clock)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not 100 <= args.runs <= 20000:
        parser.error("--runs must be between 100 and 20000")
    if sqlite3.sqlite_version_info < (3, 25):
        parser.error("SQLite 3.25 or newer is required")
    measured = measure(args.runs)
    projections = {rate: project(measured, rate) for rate in args.runs_per_day}
    if args.json:
        print(json.dumps({"measured": measured, "projected": {str(rate): value for rate, value in projections.items()}}, indent=2))
        return
    for key, value in measured.items():
        print(f"{key:34s} {value:12.1f}")
    for rate, projected in projections.items():
        print(f"projection at {rate:g} runs/day:")
        for key, value in projected.items():
            shown = f"{value:14.0f}" if key == "runs_per_year" else f"{value / 1024 / 1024:10.1f} MiB"
            print(f"  {key:36s} {shown}")
        print(f"  fraction of a 1 GiB budget          {projected['state_total_worst_case'] / GIB:10.1%}")


if __name__ == "__main__":
    main()
