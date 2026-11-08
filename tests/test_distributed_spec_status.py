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
    "PI-DEPLOY": 7, "PI-VERIFY": 13, "PI-REPO": 6, "PRD": 37,
    "API": 16, "SEC2": 20, "PK": 12, "UX": 11, "REV": 8, "DOG": 25, "PAR": 14,
    "UXR": 46,
}
SPEC_EXPLICIT_IDS = {"PI-DEPLOY-000", "DOG-017", "DOG-018", "DOG-019", "DOG-020", "DOG-021", "DOG-024", "DOG-025"}
SPEC_OMITTED_IDS = {"PI-FLASH-008", "PI-FLASH-009", "DOG-015", "DOG-016"}
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


FINAL_REPORT = REGISTER.parent / "FINAL_REPORT.md"
STATUS_MARKERS = ("[x]", "[~]", "[!]", "[ ]")
REPORT_COUNT_ROW = re.compile(r"^\| .*`(\[[ ~x!]\])`.* \| (\d+) \|$", re.MULTILINE)
REPORT_TOTAL_ROW = re.compile(r"^\| \*\*Total\*\* \| \*\*(\d+)\*\* \|$", re.MULTILINE)
REPORT_PROSE_TOTAL = re.compile(r"\bhas (\d+) (?:specification )?IDs\b")


def final_report_mismatches(report: Path = FINAL_REPORT) -> list[str]:
    """Return every disagreement between the FINAL_REPORT counts and the register."""
    register_counts = Counter(status for _, status, _ in register_rows())
    register_total = sum(register_counts.values())
    text = report.read_text(encoding="utf-8")
    start = text.index("## 2. Specification ID counts")
    end = text.index("\n## ", start + 1)
    section = text[start:end]

    mismatches: list[str] = []
    report_counts = {marker: int(count) for marker, count in REPORT_COUNT_ROW.findall(section)}
    for marker in STATUS_MARKERS:
        if marker not in report_counts:
            mismatches.append(f"FINAL_REPORT count table has no row for {marker}")
        elif report_counts[marker] != register_counts[marker]:
            mismatches.append(
                f"{marker}: FINAL_REPORT says {report_counts[marker]}, "
                f"SPEC_STATUS.md has {register_counts[marker]}"
            )

    total = REPORT_TOTAL_ROW.search(section)
    if total is None:
        mismatches.append("FINAL_REPORT count table has no Total row")
    elif int(total.group(1)) != register_total:
        mismatches.append(f"Total: FINAL_REPORT says {total.group(1)}, SPEC_STATUS.md has {register_total}")

    prose_totals = REPORT_PROSE_TOTAL.findall(text)
    if not prose_totals:
        mismatches.append("FINAL_REPORT prose has no 'has N IDs' total")
    for value in prose_totals:
        if int(value) != register_total:
            mismatches.append(
                f"prose 'has {value} IDs': FINAL_REPORT says {value}, SPEC_STATUS.md has {register_total}"
            )
    return mismatches


def test_final_report_counts_match_register() -> None:
    mismatches = final_report_mismatches()
    assert not mismatches, "FINAL_REPORT counts differ from SPEC_STATUS.md:\n" + "\n".join(mismatches)


def test_final_report_count_check_fails_on_stale_total(tmp_path: Path) -> None:
    total = len(register_rows())
    stale = tmp_path / "FINAL_REPORT.md"
    text = FINAL_REPORT.read_text(encoding="utf-8")
    stale.write_text(
        text.replace(f"| **Total** | **{total}** |", f"| **Total** | **{total - 1}** |").replace(
            f"now has {total} IDs", f"now has {total - 1} IDs"
        ),
        encoding="utf-8",
    )

    mismatches = final_report_mismatches(stale)

    assert any(f"Total: FINAL_REPORT says {total - 1}, SPEC_STATUS.md has {total}" in item for item in mismatches)
    assert any(f"prose 'has {total - 1} IDs': FINAL_REPORT says {total - 1}, SPEC_STATUS.md has {total}" in item for item in mismatches)


# A done UXR row cites a test as `tests/x.py::name` or `web/e2e/x.spec.ts` with an optional
# `::title`, all in backticks inside its Evidence clause. Screenshots are `*.png` paths.
UXR_TEST_CITATION = re.compile(r"`(tests/[\w./-]+\.py)::(\w+)`|`(web/e2e/[\w./-]+\.spec\.ts)(?:::([^`]+))?`")
SCREENSHOT_PATH = re.compile(r"[\w./-]+\.png")
VISIBLE_MARKER = "(visible)"
REPO_ROOT = REGISTER.parents[2]


def _citation_exists(match: re.Match[str], root: Path) -> bool:
    if match.group(1):
        path = root / match.group(1)
        return path.is_file() and re.search(
            rf"\bdef {re.escape(match.group(2))}\b", path.read_text(encoding="utf-8")
        ) is not None
    path = root / match.group(3)
    if not path.is_file():
        return False
    title = match.group(4)
    return title is None or title in path.read_text(encoding="utf-8")


def uxr_evidence_mismatches(
    rows: list[tuple[str, str, str]] | None = None, root: Path = REPO_ROOT
) -> list[str]:
    """Return why each done UXR row lacks an existing test, a revert statement or a screenshot."""
    mismatches: list[str] = []
    for identifier, status, text in register_rows() if rows is None else rows:
        if not identifier.startswith("UXR-") or status != "[x]":
            continue
        _, found, evidence = text.partition("Evidence:")
        if not found:
            mismatches.append(f"{identifier}: no 'Evidence:' clause")
            continue
        if not any(_citation_exists(m, root) for m in UXR_TEST_CITATION.finditer(evidence)):
            mismatches.append(f"{identifier}: no existing test cited")
        if "revert" not in evidence.lower():
            mismatches.append(f"{identifier}: evidence does not say what was reverted")
        if VISIBLE_MARKER in text and not any(
            (root / path).is_file() for path in SCREENSHOT_PATH.findall(evidence)
        ):
            mismatches.append(f"{identifier}: visible row cites no existing screenshot (*.png)")
    return mismatches


def test_done_uxr_rows_cite_a_test_and_screenshots() -> None:
    mismatches = uxr_evidence_mismatches()
    assert not mismatches, "UXR rows marked [x] without proof:\n" + "\n".join(mismatches)


def test_uxr_evidence_check_passes_and_fails_on_fake_rows(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fake.py").write_text("def test_fake_row():\n    pass\n", encoding="utf-8")
    shots = tmp_path / "docs" / "distributed" / "ux-evidence" / "UXR-901"
    shots.mkdir(parents=True)
    (shots / "mobile.png").write_bytes(b"\x89PNG\r\n")
    passing = (
        "UXR-901",
        "[x]",
        "(visible) Fake row. Evidence: `tests/test_fake.py::test_fake_row`, reverted the fix so the test "
        "failed. Screenshot docs/distributed/ux-evidence/UXR-901/mobile.png.",
    )
    failing = (
        "UXR-902",
        "[x]",
        "(visible) Fake row. Evidence: `tests/test_fake.py::test_missing`, no screenshot.",
    )
    not_done = ("UXR-903", "[ ]", "(visible) Fake row. Not started.")

    assert uxr_evidence_mismatches([passing, not_done], tmp_path) == []
    assert uxr_evidence_mismatches([failing], tmp_path) == [
        "UXR-902: no existing test cited",
        "UXR-902: evidence does not say what was reverted",
        "UXR-902: visible row cites no existing screenshot (*.png)",
    ]
