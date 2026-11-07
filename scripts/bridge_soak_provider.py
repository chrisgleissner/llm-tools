#!/usr/bin/env python3
"""Fake provider CLI for the distributed soak and chaos harnesses.

Each invocation picks one behaviour from a seeded random stream and plays it, so a
mission sees a mix of output, questions, rate limits and crashes without touching a
real provider, the network or anything outside its own working folder:

  output     prints a few lines of progress, then exits 0 (the task succeeds)
  question   prints a yes/no question and waits (the scheduler aborts it as a blocked prompt)
  ratelimit  prints a provider rate-limit message and exits 1
  crash      prints a partial line and dies with SIGKILL

Environment:
  SOAK_PROVIDER_SEED     integer seed (default 0)
  SOAK_PROVIDER_MIX      comma list of weights output,question,ratelimit,crash (default 80,5,10,5)
  SOAK_PROVIDER_SPEED    multiplier for the delays (default 1; 0 removes them)
  SOAK_PROVIDER_LINES    lines printed by an ``output`` run (default 4)

The invocation counter lives in ``.soak-provider-count`` in the working folder, so two
missions in different folders draw independent streams.
"""

from __future__ import annotations

import hashlib
import os
import random
import signal
import sys
import time
from pathlib import Path

NAMES = ("output", "question", "ratelimit", "crash")


def choose(seed: int, folder: str, count: int, weights: list[float]) -> str:
    digest = hashlib.sha256(f"{seed}:{folder}:{count}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big")).choices(NAMES, weights=weights)[0]


def parse_mix(text: str) -> list[float]:
    values = [float(part) for part in text.split(",")]
    if len(values) != len(NAMES) or min(values) < 0 or sum(values) <= 0:
        raise SystemExit(f"SOAK_PROVIDER_MIX needs {len(NAMES)} non-negative weights, got {text!r}")
    return values


def pause(seconds: float, speed: float) -> None:
    if speed > 0:
        time.sleep(seconds / speed)


def main() -> int:
    seed = int(os.environ.get("SOAK_PROVIDER_SEED", "0"))
    speed = float(os.environ.get("SOAK_PROVIDER_SPEED", "1"))
    lines = int(os.environ.get("SOAK_PROVIDER_LINES", "4"))
    weights = parse_mix(os.environ.get("SOAK_PROVIDER_MIX", "80,5,10,5"))
    counter = Path(".soak-provider-count")
    try:
        count = int(counter.read_text()) + 1
    except (OSError, ValueError):
        count = 1
    counter.write_text(str(count))
    behaviour = os.environ.get("SOAK_PROVIDER_FORCE") or choose(seed, os.getcwd(), count, weights)
    print(f"soak provider run {count}: {behaviour}", flush=True)
    if behaviour == "output":
        for index in range(lines):
            print(f"step {index + 1}/{lines}: reading files, editing, running checks", flush=True)
            pause(0.4, speed)
        print("done", flush=True)
        return 0
    if behaviour == "question":
        print("Do you want to proceed with this change? (y/n)", flush=True)
        pause(3600, speed or 1)  # the scheduler aborts a stalled question long before this
        return 0
    if behaviour == "ratelimit":
        print("Error: 429 Too Many Requests: rate limit exceeded, please try again later", file=sys.stderr, flush=True)
        return 1
    print("working on the change", end="", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
    return 137


if __name__ == "__main__":
    raise SystemExit(main())
