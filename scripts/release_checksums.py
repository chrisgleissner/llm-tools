#!/usr/bin/env python3
"""Write a `sha256sum --check` compatible checksum file for every regular file in a directory.

    python scripts/release_checksums.py DIRECTORY OUTPUT

OUTPUT is excluded from its own listing. Entries are sorted by file name so the output is reproducible.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def checksum_lines(directory: Path, output: Path) -> list[str]:
    lines = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.resolve() == output.resolve():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        lines.append(f"{digest.hexdigest()}  {path.name}")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: release_checksums.py DIRECTORY OUTPUT", file=sys.stderr)
        return 2
    directory, output = Path(args[0]), Path(args[1])
    lines = checksum_lines(directory, output)
    if not lines:
        print(f"error: no files in {directory}", file=sys.stderr)
        return 1
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
