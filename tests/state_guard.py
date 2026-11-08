"""Guard against tests writing missions into the operator's real Bridge state.

Every test runs with a private ``XDG_STATE_HOME`` (``tests/conftest.py``), so the Bridge state
directory a test resolves is never the operator's. This guard checks that promise from the
outside: it records the missions in the real state databases when the session starts and, when it
ends, reports any new mission whose working directory is inside a temporary directory. Missions
the operator creates in real project folders during the session are not reported, so a concurrent
real Bridge session does not cause false failures.

The databases are opened read-only and a missing, locked or unreadable database is skipped.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path


def real_state_databases(environ: Mapping[str, str]) -> list[Path]:
    """The Bridge databases a CLI started with ``environ`` (before test isolation) would use."""
    candidates: list[Path] = []
    explicit = environ.get("LLM_BRIDGE_STATE_DIR")
    if explicit:
        candidates.append(Path(explicit) / "bridge.sqlite3")
    home = environ.get("HOME")
    state_home = environ.get("XDG_STATE_HOME") or (str(Path(home) / ".local" / "state") if home else "")
    if state_home:
        candidates.append(Path(state_home) / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    return list(dict.fromkeys(candidates))


def _missions(database: Path) -> dict[str, str] | None:
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1.0)
        try:
            return {str(row[0]): str(row[1]) for row in connection.execute("SELECT id, cwd FROM missions")}
        finally:
            connection.close()
    except sqlite3.Error:
        return None


def snapshot(databases: Iterable[Path]) -> dict[Path, set[str]]:
    """Mission IDs per readable database."""
    result: dict[Path, set[str]] = {}
    for database in databases:
        missions = _missions(database)
        if missions is not None:
            result[database] = set(missions)
    return result


def leaked_missions(baseline: Mapping[Path, set[str]], temp_roots: Iterable[str]) -> list[str]:
    """New missions, since ``baseline``, whose working directory is inside one of ``temp_roots``."""
    roots = [os.path.realpath(root).rstrip(os.sep) + os.sep for root in temp_roots if root]
    found: list[str] = []
    for database, before in baseline.items():
        missions = _missions(database) or {}
        for mission_id, cwd in sorted(missions.items()):
            resolved = os.path.realpath(cwd).rstrip(os.sep) + os.sep
            if mission_id not in before and any(resolved.startswith(root) for root in roots):
                found.append(f"{database}: mission {mission_id[:8]} (cwd {cwd})")
    return found
