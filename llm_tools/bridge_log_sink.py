"""CLI entry point for Bridge-owned tmux pane log rotation."""

from __future__ import annotations

import argparse
import sys

from .bridge_logs import sink_stdin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-bridge-log-sink")
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--rotations", required=True, type=int)
    args = parser.parse_args(argv)
    if args.max_bytes < 1024 or args.rotations < 0 or args.rotations > 20:
        print("invalid Bridge log retention values", file=sys.stderr)
        return 2
    return sink_stdin(args.path, max_bytes=args.max_bytes, rotations=args.rotations)


if __name__ == "__main__":
    raise SystemExit(main())
