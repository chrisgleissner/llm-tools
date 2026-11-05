"""TRACK-002: every distributed-architecture specification ID appears exactly once."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REGISTER = Path(__file__).resolve().parents[1] / "docs" / "distributed" / "SPEC_STATUS.md"

# Prefix and highest number of each ID group in the distributed Bridge specification.
SPEC_GROUPS = {
    "AGENT": 9, "TRACK": 8, "REPO": 5, "ARCH": 9, "LOCAL": 8, "CLI": 8, "WORK": 10, "COORD": 10, "PROTO": 10,
    "NET": 10, "AUTH": 10, "SEC": 9, "WEB": 12, "DATA": 7, "OPS": 5, "TEST": 17, "QUAL": 10, "CONV": 12,
}
ROW = re.compile(r"^\| ([A-Z]+-\d{3}) \| (\[[ ~x!]\]) \| (.*) \|$")


def expected_ids() -> set[str]:
    return {f"{prefix}-{number:03d}" for prefix, count in SPEC_GROUPS.items() for number in range(1, count + 1)}


def register_rows() -> list[tuple[str, str, str]]:
    rows = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\| [A-Z]+-\d", line):
            match = ROW.match(line)
            assert match, f"malformed register row: {line}"
            rows.append((match.group(1), match.group(2), match.group(3).strip()))
    return rows


def test_every_spec_id_is_listed_exactly_once() -> None:
    counts = Counter(identifier for identifier, _, _ in register_rows())
    duplicates = sorted(identifier for identifier, count in counts.items() if count > 1)
    missing = sorted(expected_ids() - set(counts))
    unknown = sorted(set(counts) - expected_ids())
    assert (duplicates, missing, unknown) == ([], [], [])


def test_verified_and_blocked_rows_carry_evidence() -> None:
    for identifier, status, evidence in register_rows():
        if status in {"[x]", "[!]"}:
            assert evidence, f"{identifier} is {status} without evidence or blocker"
