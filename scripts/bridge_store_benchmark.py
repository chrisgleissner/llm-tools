#!/usr/bin/env python3
"""Reproducible local SQLite load measurement; never launches a provider."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm_tools.bridge_store import BridgeStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.tasks <= 100000:
        parser.error("--tasks must be between 1 and 100000")
    result = {"tasks": args.tasks, "provider_calls": 0}
    cpu_start = time.process_time()
    with tempfile.TemporaryDirectory(prefix="bridge-bench-") as directory:
        root = Path(directory)
        path = root / "bridge.sqlite3"
        with BridgeStore(path) as store:
            mid = store.create_mission("benchmark", "objective", cwd=root)["id"]
            started = time.perf_counter()
            for index in range(args.tasks):
                store.add_task(mid, f"task {index}", priority=index % 8)
            result["insert_seconds"] = round(time.perf_counter() - started, 6)
            started = time.perf_counter()
            tasks = store.list_tasks(mid)
            result["list_seconds"] = round(time.perf_counter() - started, 6)
            assert len(tasks) == args.tasks
            started = time.perf_counter()
            command = store.enqueue_command(mid, {"action": "pause"})
            result["command_commit_seconds"] = round(time.perf_counter() - started, 6)
            assert command["status"] == "QUEUED"
            result["state_bytes_including_wal"] = sum(
                file.stat().st_size for file in root.iterdir() if file.is_file())
            result["integrity_check"] = store.db.execute("PRAGMA integrity_check").fetchone()[0]
        result["database_bytes_after_close"] = path.stat().st_size
    result["cpu_seconds"] = round(time.process_time() - cpu_start, 6)
    result["peak_rss_platform_units"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["note"] = "Store-only local measurement; does not establish TUI latency or long-duration memory behavior"
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
