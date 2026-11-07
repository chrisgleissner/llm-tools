"""Validate the distributed register's unique IDs, evidence, and public examples."""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest

REGISTER = Path(__file__).resolve().parents[1] / "docs" / "distributed" / "SPEC_STATUS.md"
FINAL_REPORT = REGISTER.with_name("FINAL_REPORT.md")

# Prefix and highest number of each ID group in the distributed Bridge specification.
SPEC_GROUPS = {
    "AGENT": 9, "TRACK": 12, "REPO": 5, "ARCH": 9, "LOCAL": 8, "CLI": 8, "WORK": 10, "COORD": 10, "PROTO": 10,
    "NET": 10, "AUTH": 10, "SEC": 9, "WEB": 12, "DATA": 7, "OPS": 5, "TEST": 17, "QUAL": 10, "CONV": 12,
    "PI-FLASH": 13, "PI-BOOT": 5, "PI-HARD": 6, "PI-SYS": 6, "PI-WEAR": 10, "PI-UPD": 9,
    "PI-DEPLOY": 7, "PI-VERIFY": 13, "PI-REPO": 6,
    "API": 16, "SEC2": 20, "PK": 12, "UX": 11, "REV": 8, "DOG": 14, "PAR": 14,
}
SPEC_EXPLICIT_IDS = {"PI-DEPLOY-000"}
SPEC_OMITTED_IDS = {"PI-FLASH-008", "PI-FLASH-009"}
ID_ROW = re.compile(r"^\| ([A-Za-z0-9][A-Za-z0-9-]*-\d{3}) \|")
ID_PATTERN = r"[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)?-\d{3}"
ROW = re.compile(rf"^\| ({ID_PATTERN}) \| (\[[ ~x!]\]) \|(.*)\|$")
PRIVATE_ADDRESS = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})(?:/\d{1,2})?\b"
)
ALLOWED_PRIVATE_EXAMPLES = {
    "192.168.1.20",
    "192.168.1.0/24",
    "172.17.0.0/16",
    "172.29.0.0/16",
    "172.30.1.1/24",
}


def expected_ids() -> set[str]:
    generated = {f"{prefix}-{number:03d}" for prefix, count in SPEC_GROUPS.items() for number in range(1, count + 1)}
    return (generated - SPEC_OMITTED_IDS) | SPEC_EXPLICIT_IDS


def register_rows() -> list[tuple[str, str, str]]:
    rows = []
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if ID_ROW.match(line):
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


def test_final_report_counts_match_the_register() -> None:
    counts = Counter(status for _, status, _ in register_rows())
    labels = {"[x]": "Verified", "[~]": "Incomplete", "[!]": "Blocked", "[ ]": "Not started"}
    report = FINAL_REPORT.read_text(encoding="utf-8")
    for status, label in labels.items():
        assert f"| {label} `[{status[1]}]` | {counts[status]} |" in report
    assert f"| **Total** | **{sum(counts.values())}** |" in report


def test_numeric_prefix_id_is_not_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    register = tmp_path / "SPEC_STATUS.md"
    register.write_text("| SEC2-001 | [ ] | |\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REGISTER", register)

    assert register_rows() == [("SEC2-001", "[ ]", "")]


def test_malformed_id_row_fails_instead_of_being_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    register = tmp_path / "SPEC_STATUS.md"
    register.write_text("| SEC2-SUB-SUB-001 | [ ] | |\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "REGISTER", register)

    with pytest.raises(AssertionError, match="malformed register row"):
        register_rows()


def test_distributed_docs_do_not_contain_unapproved_private_addresses() -> None:
    documents = [REGISTER, *REGISTER.parent.glob("*.md")]
    violations = []
    for document in sorted(set(documents)):
        text = document.read_text(encoding="utf-8")
        for token in PRIVATE_ADDRESS.findall(text):
            if token not in ALLOWED_PRIVATE_EXAMPLES:
                violations.append(f"{document.name}: {token}")
    assert not violations, "private deployment address in distributed docs: " + ", ".join(violations)
