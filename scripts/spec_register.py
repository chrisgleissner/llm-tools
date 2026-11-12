"""Parser and rule checks for the distributed specification register (standard library only).

Shared by ``tests/test_distributed_spec_status.py`` and ``scripts/spec_open.py``.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER = REPO_ROOT / "docs" / "distributed" / "SPEC_STATUS.md"
DECISIONS = REGISTER.with_name("DECISIONS.md")

ID_PATTERN = r"[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)?-\d{3}"
STATUSES = ("[x]", "[w]", "[d]", "[~]", "[!]", "[ ]")
ID_ROW = re.compile(r"^\| ([A-Za-z0-9][A-Za-z0-9-]*-\d{3}) \|")
ROW = re.compile(rf"^\| ({ID_PATTERN}) \| (\[[ ~x!wd]\]) \|(.*)\|$")
DECISION_CITATION = re.compile(r"\bD(\d+)\b")
DECISION_HEADING = re.compile(r"^## D(\d+)\b", re.MULTILINE)
TARGET = re.compile(r"\btarget (\d+\.\d+\.\d+)\b")

LATER_RELEASE_FAMILIES = {"PROV", "ORCH"}
LATER_RELEASE_IDS = {"LB-004"}
RELEASES = ("1.5.0", "1.6.0")


class Row(NamedTuple):
    identifier: str
    status: str
    text: str


def parse_rows(register: Path = REGISTER) -> list[Row]:
    """Return every register row; a row that looks like an ID row but does not parse is an error."""
    rows = []
    for line in register.read_text(encoding="utf-8").splitlines():
        if ID_ROW.match(line):
            match = ROW.match(line)
            assert match, f"malformed register row: {line}"
            rows.append(Row(match.group(1), match.group(2), match.group(3).strip()))
    return rows


def family(identifier: str) -> str:
    return identifier.rsplit("-", 1)[0]


def row_release(identifier: str) -> str:
    """The release that an ID gates: PROV, ORCH and LB-004 gate 1.6.0, everything else 1.5.0."""
    if identifier in LATER_RELEASE_IDS or family(identifier) in LATER_RELEASE_FAMILIES:
        return "1.6.0"
    return "1.5.0"


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def is_open(row: Row, release: str) -> bool:
    """Whether a row is not yet closed for ``release`` (``[d]`` is closed only before its target)."""
    if row.status in {"[x]", "[w]"}:
        return False
    if row.status == "[d]":
        target = TARGET.search(row.text)
        return target is None or version_key(target.group(1)) <= version_key(release)
    return True


def open_rows(rows: list[Row], release: str) -> list[Row]:
    """Rows still open for ``release``. 1.5.0 checks only 1.5.0 rows; 1.6.0 checks all rows."""
    if release not in RELEASES:
        raise ValueError(f"unknown release {release!r}")
    considered = [r for r in rows if release == "1.6.0" or row_release(r.identifier) == "1.5.0"]
    return [r for r in considered if is_open(r, release)]


def status_citation_problems(rows: list[Row]) -> list[str]:
    """TRACK-013: a ``[w]`` row cites a decision; a ``[d]`` row cites a decision and a target release."""
    problems = []
    for row in rows:
        if row.status in {"[w]", "[d]"} and not DECISION_CITATION.search(row.text):
            problems.append(f"{row.identifier}: {row.status} row cites no decision (D<number>)")
        if row.status == "[d]" and not TARGET.search(row.text):
            problems.append(f"{row.identifier}: [d] row has no 'target <x.y.z>'")
    return problems


def decision_problems(register_text: str, decisions_text: str) -> list[str]:
    """TRACK-017: decision headings are unique and every decision cited in the register exists."""
    numbers = DECISION_HEADING.findall(decisions_text)
    problems = [f"duplicate decision heading D{n}" for n, c in Counter(numbers).items() if c > 1]
    known = set(numbers)
    cited = set(DECISION_CITATION.findall(register_text))
    problems += [f"D{n} is cited in the register but has no heading" for n in sorted(cited - known, key=int)]
    return problems


_NAME = r"[\w./-]+::(?:\"[^\"]+\"|\w+)"
_POINTER = rf"`?{_NAME}`?"
TESTS_TAIL = re.compile(rf"Tests: ({_POINTER}(?:, {_POINTER})*)\.?$")
EVIDENCE_TAIL = re.compile(r"Evidence: `?([\w./-]+)`?\.?$")
_POINTER_PARTS = re.compile(r"([\w./-]+)::(?:\"([^\"]+)\"|(\w+))")


def pointer_problem(path: str, name: str, root: Path) -> str | None:
    file = root / path
    if not file.is_file():
        return f"{path} does not exist"
    text = file.read_text(encoding="utf-8")
    if file.suffix == ".py":
        found = re.search(rf"\bdef {re.escape(name)}\b", text) is not None
    elif file.suffix == ".rs":
        found = re.search(rf"\bfn {re.escape(name)}\b", text) is not None
    else:
        found = name in text
    return None if found else f"{path} does not contain {name}"


def pointer_check(rows: list[Row], frozen: set[str], root: Path = REPO_ROOT) -> list[str]:
    """TRACK-014: ``[x]`` rows outside the frozen list end with checked ``Tests:`` or ``Evidence:``."""
    problems = []
    for row in rows:
        if row.status != "[x]" or row.identifier in frozen:
            continue
        tests = TESTS_TAIL.search(row.text)
        evidence = EVIDENCE_TAIL.search(row.text)
        if tests:
            for path, quoted, plain in _POINTER_PARTS.findall(tests.group(1)):
                problem = pointer_problem(path, quoted or plain, root)
                if problem:
                    problems.append(f"{row.identifier}: {problem}")
        elif evidence:
            if not (root / evidence.group(1)).exists():
                problems.append(f"{row.identifier}: evidence path {evidence.group(1)} does not exist")
        else:
            problems.append(f"{row.identifier}: [x] row does not end with 'Tests: <pointer>' or 'Evidence: <path>'")
    return problems


REQUIREMENT_LINE = re.compile(rf"^- ({ID_PATTERN}): \S.*$")


def requirement_problems(rows: list[Row], spec_dir: Path) -> list[str]:
    """TRACK-015: each register ID has exactly one requirement line and each line has a register row."""
    lines: Counter[str] = Counter()
    for file in sorted(spec_dir.glob("*.md")):
        for line in file.read_text(encoding="utf-8").splitlines():
            match = REQUIREMENT_LINE.match(line)
            if match:
                lines[match.group(1)] += 1
    ids = {row.identifier for row in rows}
    problems = [f"{i}: no requirement line" for i in sorted(ids - set(lines))]
    problems += [f"{i}: requirement line without a register row" for i in sorted(set(lines) - ids)]
    problems += [f"{i}: {n} requirement lines" for i, n in sorted(lines.items()) if n > 1]
    return problems


FINDING_PATTERNS = {
    "ux-review-web.md": re.compile(r"^#+ (WUX-\d{3})\b"),
    "review-internals.md": re.compile(r"^#+ (INT-\d{3})\b"),
    "ux-review-cli.md": re.compile(r"^#+ (CLIUX-\d{3})\b"),
    "review-findings.md": re.compile(r"^#+ (ZAP-\d{3})\b"),
    "capacity-plan.md": re.compile(r"^(F\d+)\. "),
}
DISPOSITION_ROW = re.compile(r"^\| ([A-Z][A-Z0-9]*-?\d+) \| (.+) \|$")
DISPOSITION_OK = re.compile(rf"^(?:fixed|no change|{ID_PATTERN})\b")


def disposition_problems(document: Path, finding: re.Pattern[str]) -> list[str]:
    """TRACK-016: each finding in ``document`` has a valid row in its '## Dispositions' table."""
    text = document.read_text(encoding="utf-8")
    findings = [m.group(1) for line in text.splitlines() if (m := finding.match(line))]
    _, found, tail = text.partition("\n## Dispositions")
    table: dict[str, str] = {}
    if found:
        section = re.split(r"\n## ", tail, maxsplit=1)[0]
        for line in section.splitlines():
            row = DISPOSITION_ROW.match(line)
            if row:
                table[row.group(1)] = row.group(2).strip()
    problems = [] if found else [f"{document.name}: no '## Dispositions' section"]
    if not findings:
        problems.append(f"{document.name}: no findings found")
    for item in findings:
        if item not in table:
            problems.append(f"{document.name}: {item} has no disposition")
        elif not DISPOSITION_OK.match(table[item]):
            problems.append(f"{document.name}: {item} disposition must start with fixed, no change or a register ID")
    return problems
