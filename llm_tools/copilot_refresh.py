from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import common


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return 2
    cache = Path(args[0])
    # The launcher put the package root in PYTHONPATH because it runs us with
    # `python -P`. The Copilot CLI that read_copilot_live starts does not need it.
    cleaned = common.without_package_root(os.environ)
    os.environ.clear()
    os.environ.update(cleaned)
    lock = cache.with_name("copilot-refresh.lock")
    try:
        result = common.read_copilot_live(os.environ)
        if result:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(result, separators=(",", ":")) + "\n", encoding="utf-8")
            tmp.replace(cache)
        return 0
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
