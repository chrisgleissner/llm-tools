"""Golden-file storage and comparison for rendered Bridge screens.

A golden file holds one screen (one *case*) as

* ``rows``      - the text of every screen row, right-trimmed, one string per row,
* ``segments``  - per row, a list of ``[column, text, role]`` runs, where ``role`` is the
                  semantic Bridge colour role (``brand``, ``state_running``, ``selected`` ...)
                  and may carry ``+flag`` suffixes for extra attributes (``+underline``),
* ``cursor``    - the last cursor position and whether the cursor was visible.

Files are plain JSON laid out one row per line, so a diff of a golden file reads like a diff
of the screen.  See ``tests/test_bridge_golden_screens.py`` for the cases and
``AGENTS.md`` ("Golden screens") for when to regenerate them.

``UPDATE_GOLDEN=1`` rewrites the files instead of comparing them.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Any

from .fakes import Frame

GOLDEN_DIR = Path(__file__).parent / "golden" / "screens"
UPDATE_ENV = "UPDATE_GOLDEN"
FORMAT_VERSION = 1
_MAX_DIFF_ROWS = 40


def updating() -> bool:
    return os.environ.get(UPDATE_ENV, "").strip().lower() not in {"", "0", "false", "no"}


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def frame_to_record(name: str, frame: Frame) -> dict[str, Any]:
    return {
        "format": FORMAT_VERSION,
        "case": name,
        "size": [frame.height, frame.width],
        "cursor": None if frame.cursor is None else list(frame.cursor),
        "cursor_visible": frame.cursor_visible,
        "rows": list(frame.rows),
        "segments": [[[column, text, role] for column, text, role in row] for row in frame.segments],
    }


def dumps(record: dict[str, Any]) -> str:
    """Stable text form: scalar fields on one line each, then one line per row."""

    def one(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))

    lines = ["{"]
    for key in ("format", "case", "size", "cursor", "cursor_visible"):
        lines.append(f"  {one(key)}: {one(record[key])},")
    lines.append('  "rows": [')
    lines.extend(f"    {one(row)}{',' if index < len(record['rows']) - 1 else ''}" for index, row in enumerate(record["rows"]))
    lines.append("  ],")
    lines.append('  "segments": [')
    lines.extend(f"    {one(row)}{',' if index < len(record['segments']) - 1 else ''}" for index, row in enumerate(record["segments"]))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _numbered(rows: list[str]) -> list[str]:
    return [f"{index:02d}| {row}" for index, row in enumerate(rows)]


def _segment_key(run: list[Any]) -> tuple[int, str, str]:
    return int(run[0]), str(run[1]), str(run[2])


def diff_records(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """Readable difference between two records: text rows as a unified diff, then role changes."""
    parts: list[str] = []
    for key in ("size", "cursor", "cursor_visible"):
        if expected.get(key) != actual.get(key):
            parts.append(f"{key}: expected {expected.get(key)!r}, got {actual.get(key)!r}")
    text_diff = list(difflib.unified_diff(
        _numbered(expected["rows"]), _numbered(actual["rows"]), "golden rows", "actual rows", lineterm="", n=1,
    ))
    if text_diff:
        parts.append("text changes:\n" + "\n".join(text_diff))
    role_lines: list[str] = []
    for index in range(max(len(expected["segments"]), len(actual["segments"]))):
        old = [_segment_key(run) for run in (expected["segments"][index] if index < len(expected["segments"]) else [])]
        new = [_segment_key(run) for run in (actual["segments"][index] if index < len(actual["segments"]) else [])]
        if old == new:
            continue
        if len(role_lines) >= _MAX_DIFF_ROWS:
            role_lines.append("... more rows differ")
            break
        same_text = expected["rows"][index:index + 1] == actual["rows"][index:index + 1]
        # Report each run only on the side where it differs, with the role that changed.
        old_roles = {(column, text): role for column, text, role in old}
        new_roles = {(column, text): role for column, text, role in new}
        changes = []
        for (column, text), role in new_roles.items():
            before = old_roles.get((column, text))
            if before is None:
                changes.append(f"    + col {column} {text!r} [{role}]")
            elif before != role:
                changes.append(f"    ~ col {column} {text!r} role {before} -> {role}")
        for (column, text), role in old_roles.items():
            if (column, text) not in new_roles:
                changes.append(f"    - col {column} {text!r} [{role}]")
        role_lines.append(f"  row {index:02d}{'' if same_text else ' (text also changed)'}:")
        role_lines.extend(changes)
    if role_lines:
        parts.append("role / run changes:\n" + "\n".join(role_lines))
    return "\n".join(parts)


def check(name: str, frame: Frame) -> str | None:
    """Compare ``frame`` with the golden file ``name``.

    Returns ``None`` when it matches (or the file was just rewritten under ``UPDATE_GOLDEN=1``),
    else a message describing the difference.
    """
    record = frame_to_record(name, frame)
    text = dumps(record)
    path = golden_path(name)
    if updating():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        return None
    if not path.exists():
        return f"no golden file tests/golden/screens/{path.name}; create it with {UPDATE_ENV}=1 after reviewing the screen:\n" + "\n".join(record["rows"])
    stored = path.read_text(encoding="utf-8")
    if stored == text:
        return None
    try:
        expected = json.loads(stored)
    except ValueError:
        return f"golden file {path.name} is not valid JSON; regenerate with {UPDATE_ENV}=1"
    return f"screen '{name}' differs from tests/golden/screens/{path.name}\n" + diff_records(expected, record)


def stale_goldens(expected_names: set[str]) -> list[Path]:
    """Golden files that no current case produces (a renamed or removed case)."""
    if not GOLDEN_DIR.exists():
        return []
    return sorted(path for path in GOLDEN_DIR.glob("*.json") if path.stem not in expected_names)


def remove_stale(paths: list[Path]) -> None:
    for path in paths:
        path.unlink()
