"""Source must stay valid on every supported interpreter (requires-python >= 3.11).

Python 3.12 (PEP 701) allows backslashes, the enclosing quote and comments inside the
expression part of an f-string. Python 3.11 rejects all of them with a SyntaxError that only
appears at import time on a 3.11 runner. This test finds those constructs on any 3.12+
interpreter, so a change cannot reach CI with one.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TREES = ("llm_tools", "scripts", "tests", "docs")
ROOT_FILES = ("llm-usage", "llm-scheduler", "ralph-robin", "llm-sleep-soak", "llm-bridge", "sitecustomize.py")
PREFIX = re.compile(r"^[A-Za-z]*(\"\"\"|'''|\"|')")


def _sources() -> list[Path]:
    paths = [p for tree in TREES for p in sorted((ROOT / tree).rglob("*.py"))]
    paths += [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    return paths


def find_pep701_only_fstrings(source: str) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        whole = ast.get_source_segment(source, node)
        match = PREFIX.match(whole or "")
        if not whole or not match:
            continue
        delimiter = match.group(1)
        for value in node.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            segment = ast.get_source_segment(source, value.value) or ""
            if "\\" in segment:
                problems.append((value.value.lineno, "backslash in f-string expression"))
            elif delimiter in segment:
                problems.append((value.value.lineno, "f-string expression reuses the enclosing quote"))
            elif "#" in segment and "'" not in segment and '"' not in segment:
                problems.append((value.value.lineno, "comment in f-string expression"))
    return problems


def test_detector_flags_the_constructs_python_311_rejects() -> None:
    if sys.version_info < (3, 12):
        pytest.skip("the detector reads PEP 701 source positions")
    assert find_pep701_only_fstrings("x = f\"{'a\\n'}\"\n") == [(1, "backslash in f-string expression")]
    assert find_pep701_only_fstrings("x = f\"{d[\"k\"]}\"\n") == [(1, "f-string expression reuses the enclosing quote")]
    assert find_pep701_only_fstrings("x = f\"{d['k']} {1:>{2}}\"\ny = f'''{d[\"k\"]}'''\n") == []


def test_no_fstring_needs_python_312_syntax() -> None:
    if sys.version_info < (3, 12):
        pytest.skip("on 3.11 the import itself fails if there is such an f-string")
    offenders = []
    for path in _sources():
        for line, reason in find_pep701_only_fstrings(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {reason}")
    assert offenders == []
