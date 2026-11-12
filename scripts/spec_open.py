#!/usr/bin/env python3
"""Print the specification IDs that are still open for a release (standard library only)."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spec_register  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, choices=spec_register.RELEASES)
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--register", type=Path, default=spec_register.REGISTER, help="register file")
    args = parser.parse_args(argv)

    grouped: dict[str, list[str]] = defaultdict(list)
    for row in spec_register.open_rows(spec_register.parse_rows(args.register), args.release):
        grouped[spec_register.family(row.identifier)].append(row.identifier)
    count = sum(len(ids) for ids in grouped.values())
    if args.json:
        print(json.dumps({"release": args.release, "count": count, "open": grouped}, indent=2))
    else:
        for name in sorted(grouped):
            print(f"{name}: {' '.join(grouped[name])}")
        print(f"{count} open for release {args.release}")
    return 1 if count else 0


if __name__ == "__main__":
    raise SystemExit(main())
