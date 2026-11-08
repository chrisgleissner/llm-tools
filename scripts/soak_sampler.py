#!/usr/bin/env python3
"""Sample the resident memory of every mission controller and worker of a soak run into a CSV file.

``bridge_soak.py`` records only sums over the fleet. This sampler writes one row per process and sample, so a
plot can show each controller (``soak_plot.py``) and a regression in a single process is not hidden in a sum.

    python scripts/soak_sampler.py rss.csv 30 /tmp/lbw- /tmp/lbr-

The arguments are the output file, the interval in seconds and the prefixes of the private runtime directories
(``XDG_RUNTIME_DIR``) of the processes to follow: ``/tmp/lbw-`` for the workers of ``bridge_soak.py``. The soak
harness gives each worker such a directory, and its controllers inherit it. Columns:
``t_s,kind,pid,mission,rss_kib,anon_kib,threads,fds``. It runs until it is stopped.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path


def kind_of(cmdline: bytes) -> str | None:
    if b"_controller" in cmdline:
        return "controller"
    if b"worker" in cmdline and b"run" in cmdline and (b"llm-bridge" in cmdline or b"llm_tools.bridge" in cmdline):
        return "worker"
    return None


def process_stats(pid: int) -> tuple[int, int, int, int]:
    rss = anon = threads = 0
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS"):
            rss = int(line.split()[1])
        elif line.startswith("RssAnon"):
            anon = int(line.split()[1])
        elif line.startswith("Threads"):
            threads = int(line.split()[1])
    return rss, anon, threads, len(os.listdir(f"/proc/{pid}/fd"))


def sample_once(prefixes: tuple[str, ...], now: float) -> list[str]:
    rows = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
            kind = kind_of(cmdline)
            if not kind:
                continue
            environment = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
            if not any(item.startswith(b"XDG_RUNTIME_DIR=") and item[16:].decode().startswith(prefixes) for item in environment):
                continue
            mission = re.search(rb"--mission\0([0-9a-f]{8})", cmdline)
            rss, anon, threads, fds = process_stats(int(entry))
            rows.append(f"{now:.0f},{kind},{entry},{mission.group(1).decode() if mission else ''},{rss},{anon},{threads},{fds}\n")
        except (OSError, ValueError):
            pass
    return rows


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    output, interval, prefixes = argv[1], float(argv[2]), tuple(argv[3:])
    start = time.time()
    with open(output, "a") as handle:
        if handle.tell() == 0:
            handle.write("t_s,kind,pid,mission,rss_kib,anon_kib,threads,fds\n")
        while True:
            now = time.time() - start
            handle.writelines(sample_once(prefixes, now))
            handle.flush()
            time.sleep(max(0.0, interval - ((time.time() - start) - now)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
