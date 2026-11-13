"""LB-004: no Bridge module grows past the size ceiling.

A module that needs more room is split along a seam (see the module map in AGENTS.md), not
given a higher ceiling here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "llm_tools"
MAX_LINES = 3000
MODULES = sorted(PACKAGE.glob("bridge*.py"))


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def oversized(modules: list[Path], ceiling: int) -> list[str]:
    return [f"{path.name} has {line_count(path)} lines (ceiling {ceiling})" for path in modules if line_count(path) > ceiling]


def test_bridge_modules_are_found() -> None:
    assert PACKAGE / "bridge.py" in MODULES and PACKAGE / "bridge_tui.py" in MODULES


@pytest.mark.parametrize("path", MODULES, ids=lambda path: path.name)
def test_bridge_module_stays_under_the_ceiling(path: Path) -> None:
    assert line_count(path) <= MAX_LINES, f"{path.name} has {line_count(path)} lines; the ceiling is {MAX_LINES}"


def test_the_check_reports_a_module_over_the_ceiling() -> None:
    largest = max(MODULES, key=line_count)
    assert oversized(MODULES, line_count(largest) - 1) == [f"{largest.name} has {line_count(largest)} lines (ceiling {line_count(largest) - 1})"]
