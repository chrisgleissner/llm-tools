#!/usr/bin/env python3
"""Long-running seeded fuzz runner for LLM Bridge.

Runs the same targets as ``tests/test_bridge_fuzz*.py`` (see ``tests/fuzz/``),
but continuously and with fresh seeds, and saves every failing case as a JSON
file that reproduces with ``--replay``.

    python scripts/bridge_fuzz.py --list
    python scripts/bridge_fuzz.py --seconds 600 --seed 1000 --target console_keys
    python scripts/bridge_fuzz.py --seconds 60                      # every target, shared time
    python scripts/bridge_fuzz.py --replay ~/.cache/llm-tools/bridge-fuzz/cases/console_keys-seed1234.json

Standard library only; nothing here talks to a provider, tmux server or the
network. Each run uses a private temporary state directory, HOME and PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.fuzz.harness import Failure, Sandbox, run_case  # noqa: E402
from tests.fuzz.targets import TARGETS, fresh_sandbox_root, save_failure  # noqa: E402


def default_case_dir() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "llm-tools" / "bridge-fuzz" / "cases"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seeded fuzz runner for LLM Bridge.")
    parser.add_argument("--seconds", type=float, default=60.0, help="total wall-clock budget, shared equally by the selected targets (default 60)")
    parser.add_argument("--seed", type=int, default=0, help="first seed; each target continues with seed+1, seed+2, ... (default 0)")
    parser.add_argument("--target", action="append", default=[], metavar="NAME", help="target to run (repeatable or comma separated; default: all)")
    parser.add_argument("--list", action="store_true", help="list targets and exit")
    parser.add_argument("--replay", metavar="FILE", help="re-run one saved case file and print its traceback")
    parser.add_argument("--case", metavar="TARGET:SEED", help="re-run one case by target and seed (as printed by the pytest suite) and print its traceback")
    parser.add_argument("--out", type=Path, default=None, help=f"directory for failing-case files (default {default_case_dir()})")
    parser.add_argument("--max-failures", type=int, default=25, help="stop a target after this many distinct failures (default 25)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary sandbox directory")
    return parser.parse_args(argv)


def signature(failure: Failure) -> str:
    """Group failures by exception type and innermost frames so one bug is saved once."""
    lines = [line.strip() for line in failure.trace.splitlines() if line.strip().startswith("File ")]
    tail = lines[-2:]
    last = failure.trace.strip().splitlines()[-1][:80] if failure.trace.strip() else failure.message[:80]
    return f"{failure.kind}|{last}|{'|'.join(tail)}"


def replay(name: str, seed: int) -> int:
    target = TARGETS[name]
    root = fresh_sandbox_root()
    try:
        with Sandbox(root) as sandbox:
            failure = run_case(target.name, target.function, seed, sandbox, timeout=target.case_timeout)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if failure is None:
        print(f"{target.name} seed={seed}: passes")
        return 0
    print(failure.trace)
    print("\n".join(failure.notes))
    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.list:
        for target in TARGETS.values():
            print(f"{target.name:24} {target.summary}")
        return 0
    if args.replay:
        data = json.loads(Path(args.replay).expanduser().read_text(encoding="utf-8"))
        return replay(data["target"], int(data["seed"]))
    if args.case:
        name, _, seed = args.case.partition(":")
        if name not in TARGETS or not seed.lstrip("-").isdigit():
            print(f"--case wants TARGET:SEED with a target from --list, got {args.case!r}", file=sys.stderr)
            return 2
        return replay(name, int(seed))
    names = [name for item in args.target for name in item.split(",") if name] or list(TARGETS)
    unknown = [name for name in names if name not in TARGETS]
    if unknown:
        print(f"unknown target(s): {', '.join(unknown)}; use --list", file=sys.stderr)
        return 2
    out = (args.out or default_case_dir()).expanduser()
    per_target = args.seconds / len(names)
    root = fresh_sandbox_root()
    summary: list[str] = []
    total_failures = 0
    try:
        with Sandbox(root) as sandbox:
            for name in names:
                target = TARGETS[name]
                started = time.monotonic()
                seed = args.seed
                ran = 0
                seen: dict[str, int] = {}
                while time.monotonic() - started < per_target and len(seen) < args.max_failures:
                    failure = run_case(name, target.function, seed, sandbox, timeout=target.case_timeout)
                    ran += 1
                    if failure is not None:
                        key = signature(failure)
                        seen[key] = seen.get(key, 0) + 1
                        if seen[key] == 1:
                            path = save_failure(failure, out)
                            print(f"FAIL {failure.summary()}\n     saved {path}", flush=True)
                    seed += 1
                elapsed = time.monotonic() - started
                total_failures += sum(seen.values())
                summary.append(f"{name:24} {ran:7d} cases  {elapsed:7.1f}s  seeds {args.seed}..{seed - 1}  {len(seen)} distinct failure(s), {sum(seen.values())} total")
                print(summary[-1], flush=True)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    print("\n".join(["", "summary:", *summary]))
    return 1 if total_failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(1)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(3)
