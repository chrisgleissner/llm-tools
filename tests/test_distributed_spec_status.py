"""Validate the distributed register's unique IDs, evidence, and public examples."""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import spec_register  # noqa: E402

REGISTER = Path(__file__).resolve().parents[1] / "docs" / "distributed" / "SPEC_STATUS.md"
FINAL_REPORT = REGISTER.with_name("FINAL_REPORT.md")
DECISIONS = REGISTER.with_name("DECISIONS.md")
SPEC_DIR = REGISTER.parent / "spec"
LEGACY_VERIFIED = Path(__file__).with_name("spec_legacy_verified.txt")
# The frozen list of TRACK-014 may only shrink. Lower this constant when a row leaves the list.
LEGACY_VERIFIED_COUNT = 259

# Prefix and highest number of each ID group in the distributed Bridge specification.
SPEC_GROUPS = {
    "AGENT": 9, "TRACK": 18, "REPO": 5, "ARCH": 9, "LOCAL": 8, "CLI": 8, "WORK": 10, "COORD": 10, "PROTO": 10,
    "NET": 10, "AUTH": 10, "SEC": 9, "WEB": 12, "DATA": 7, "OPS": 5, "TEST": 17, "QUAL": 10, "CONV": 12,
    "PI-FLASH": 13, "PI-BOOT": 5, "PI-HARD": 6, "PI-SYS": 6, "PI-WEAR": 10, "PI-UPD": 9,
    "PI-DEPLOY": 7, "PI-VERIFY": 13, "PI-REPO": 6, "PRD": 38,
    "API": 16, "SEC2": 20, "PK": 12, "UX": 11, "REV": 8, "DOG": 28, "PAR": 14,
    "UXR": 57, "FIND": 20, "PROV": 13, "ORCH": 14, "LB": 4, "DOCS": 4,
}
SPEC_EXPLICIT_IDS = {"PI-DEPLOY-000", "DOG-017", "DOG-018", "DOG-019", "DOG-020", "DOG-021", "DOG-024", "DOG-025", "DOG-026"}
SPEC_OMITTED_IDS = {"PI-FLASH-008", "PI-FLASH-009", "DOG-015", "DOG-016"}
ID_ROW = spec_register.ID_ROW
ID_PATTERN = spec_register.ID_PATTERN
ROW = spec_register.ROW
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
    return [tuple(row) for row in spec_register.parse_rows(REGISTER)]  # type: ignore[misc]


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
    labels = {"[x]": "Verified", "[w]": "Waived", "[d]": "Deferred", "[~]": "Incomplete", "[!]": "Blocked", "[ ]": "Not started"}
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
STATUS_MARKERS = spec_register.STATUSES
REPORT_COUNT_ROW = re.compile(r"^\| .*`(\[[ ~x!wd]\])`.* \| (\d+) \|$", re.MULTILINE)
REPORT_TOTAL_ROW = re.compile(r"^\| \*\*Total\*\* \| \*\*(\d+)\*\* \|$", re.MULTILINE)
REPORT_PROSE_TOTAL = re.compile(r"\bhas (\d+) (?:specification )?IDs\b")


def final_report_mismatches(
    report: Path = FINAL_REPORT, rows: list[tuple[str, str, str]] | None = None
) -> list[str]:
    """Return every disagreement between the FINAL_REPORT counts and the register."""
    register_counts = Counter(status for _, status, _ in (register_rows() if rows is None else rows))
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


# TRACK-013: status vocabulary -------------------------------------------------------------------

def _rows(*items: tuple[str, str, str]) -> list[spec_register.Row]:
    return [spec_register.Row(*item) for item in items]


def test_register_w_and_d_rows_parse_and_cite_decisions() -> None:
    assert spec_register.status_citation_problems(
        _rows(("PK-012", "[w]", "D46 waived"), ("PRD-022", "[d]", "D46 defer, target 1.7.0"))
    ) == []
    assert spec_register.status_citation_problems(
        _rows(("PK-012", "[w]", "waived"), ("PRD-022", "[d]", "D46 defer"), ("PRD-023", "[d]", "target 1.6.0"))
    ) == [
        "PK-012: [w] row cites no decision (D<number>)",
        "PRD-022: [d] row has no 'target <x.y.z>'",
        "PRD-023: [d] row cites no decision (D<number>)",
    ]


def test_register_row_pattern_accepts_w_and_d(tmp_path: Path) -> None:
    register = tmp_path / "SPEC_STATUS.md"
    register.write_text("| PK-012 | [w] | D46 |\n| PRD-022 | [d] | D46 target 1.7.0 |\n", encoding="utf-8")
    assert [row.status for row in spec_register.parse_rows(register)] == ["[w]", "[d]"]


def test_real_register_w_and_d_rows_cite_decisions() -> None:
    problems = spec_register.status_citation_problems(spec_register.parse_rows(REGISTER))
    assert not problems, "\n".join(problems)


def test_final_report_counts_cover_all_six_statuses(tmp_path: Path) -> None:
    rows = [("A-001", "[x]", "e"), ("A-002", "[w]", "D1"), ("A-003", "[d]", "D1 target 1.6.0"), ("A-004", "[ ]", "")]
    lines = ["Register now has 4 IDs.", "## 2. Specification ID counts"]
    for marker, label in (("x", "Verified"), ("w", "Waived"), ("d", "Deferred"), ("~", "Incomplete"),
                          ("!", "Blocked"), (" ", "Not started")):
        count = sum(1 for _, status, _ in rows if status == f"[{marker}]")
        lines.append(f"| {label} `[{marker}]` | {count} |")
    lines += ["| **Total** | **4** |", "", "## 3. Next"]
    good = tmp_path / "good.md"
    good.write_text("\n".join(lines), encoding="utf-8")
    assert final_report_mismatches(good, rows) == []

    missing = tmp_path / "missing.md"
    missing.write_text("\n".join(line for line in lines if "Waived" not in line), encoding="utf-8")
    assert "FINAL_REPORT count table has no row for [w]" in final_report_mismatches(missing, rows)

    wrong = tmp_path / "wrong.md"
    wrong.write_text("\n".join(lines).replace("| Deferred `[d]` | 1 |", "| Deferred `[d]` | 2 |"), encoding="utf-8")
    assert "[d]: FINAL_REPORT says 2, SPEC_STATUS.md has 1" in final_report_mismatches(wrong, rows)


def test_open_rows_per_release() -> None:
    rows = _rows(
        ("A-001", "[x]", "e"),
        ("A-002", "[w]", "D1"),
        ("A-003", "[d]", "D1 target 1.6.0"),
        ("A-004", "[~]", ""),
        ("PROV-001", "[ ]", ""),
        ("ORCH-001", "[d]", "D1 target 1.6.0"),
        ("LB-004", "[ ]", ""),
        ("LB-001", "[!]", ""),
    )
    assert [r.identifier for r in spec_register.open_rows(rows, "1.5.0")] == ["A-004", "LB-001"]
    assert [r.identifier for r in spec_register.open_rows(rows, "1.6.0")] == [
        "A-003", "A-004", "PROV-001", "ORCH-001", "LB-004", "LB-001",
    ]
    with pytest.raises(ValueError):
        spec_register.open_rows(rows, "9.9.9")


# TRACK-014: test pointers ----------------------------------------------------------------------

def legacy_verified() -> list[str]:
    return [line for line in LEGACY_VERIFIED.read_text(encoding="utf-8").splitlines() if line]


def test_verified_rows_outside_the_frozen_list_carry_checked_pointers() -> None:
    problems = spec_register.pointer_check(spec_register.parse_rows(REGISTER), set(legacy_verified()))
    assert not problems, "\n".join(problems)


def test_frozen_list_is_sorted_unique_and_only_shrinks() -> None:
    ids = legacy_verified()
    assert ids == sorted(set(ids))
    assert len(ids) <= LEGACY_VERIFIED_COUNT, "tests/spec_legacy_verified.txt may only shrink"
    assert len(ids) == LEGACY_VERIFIED_COUNT, "lower LEGACY_VERIFIED_COUNT to the new length of the list"
    done = {identifier for identifier, status, _ in register_rows() if status == "[x]"}
    assert sorted(set(ids) - done) == [], "frozen list contains an ID that is not [x] in the register"


def _pointer_root(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_one():\n    pass\n", encoding="utf-8")
    (tmp_path / "coordinator").mkdir()
    (tmp_path / "coordinator" / "lib.rs").write_text("fn drops_frame() {}\n", encoding="utf-8")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "x.spec.ts").write_text("test('opens the page, fast', async () => {})\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ev.md").write_text("evidence\n", encoding="utf-8")
    return tmp_path


def test_pointer_check_accepts_valid_pointers(tmp_path: Path) -> None:
    root = _pointer_root(tmp_path)
    rows = _rows(
        ("A-001", "[x]", "Done. Tests: tests/test_a.py::test_one, coordinator/lib.rs::drops_frame."),
        ("A-002", "[x]", 'Done. Tests: web/x.spec.ts::"opens the page, fast"'),
        ("A-003", "[x]", "Done. Evidence: docs/ev.md"),
        ("A-004", "[x]", "No pointer but frozen"),
        ("A-005", "[ ]", "not done"),
    )
    assert spec_register.pointer_check(rows, {"A-004"}, root) == []


def test_pointer_check_rejects_bad_pointers(tmp_path: Path) -> None:
    root = _pointer_root(tmp_path)
    rows = _rows(
        ("A-001", "[x]", "Done. Tests: tests/test_a.py::test_missing"),
        ("A-002", "[x]", "Done. Tests: tests/nope.py::test_one"),
        ("A-003", "[x]", "Done. Evidence: docs/missing.md"),
        ("A-004", "[x]", "Done with prose only"),
        ("A-005", "[x]", "Tests: tests/test_a.py::test_one, then more prose"),
    )
    no_pointer = "[x] row does not end with 'Tests: <pointer>' or 'Evidence: <path>'"
    assert spec_register.pointer_check(rows, set(), root) == [
        "A-001: tests/test_a.py does not contain test_missing",
        "A-002: tests/nope.py does not exist",
        "A-003: evidence path docs/missing.md does not exist",
        f"A-004: {no_pointer}",
        f"A-005: {no_pointer}",
    ]


# TRACK-015: requirement text -------------------------------------------------------------------

def test_every_register_id_has_one_requirement_line() -> None:
    problems = spec_register.requirement_problems(spec_register.parse_rows(REGISTER), SPEC_DIR)
    assert not problems, "\n".join(problems)


def test_requirement_check_passes_and_fails_on_fixtures(tmp_path: Path) -> None:
    rows = _rows(("AAA-001", "[ ]", ""), ("AAA-002", "[ ]", ""), ("BBB-001", "[ ]", ""))
    (tmp_path / "a.md").write_text("# A\n\n- AAA-001: first\n- AAA-002: second\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("- BBB-001: third\n", encoding="utf-8")
    assert spec_register.requirement_problems(rows, tmp_path) == []

    (tmp_path / "b.md").write_text("- BBB-001: third\n- BBB-001: again\n- CCC-001: orphan\n", encoding="utf-8")
    (tmp_path / "a.md").write_text("- AAA-001: first\n", encoding="utf-8")
    assert spec_register.requirement_problems(rows, tmp_path) == [
        "AAA-002: no requirement line",
        "CCC-001: requirement line without a register row",
        "BBB-001: 2 requirement lines",
    ]


# TRACK-016: findings are closed by the register ------------------------------------------------

@pytest.mark.parametrize("name", sorted(spec_register.FINDING_PATTERNS))
def test_every_finding_has_a_disposition(name: str) -> None:
    problems = spec_register.disposition_problems(REGISTER.parent / name, spec_register.FINDING_PATTERNS[name])
    assert not problems, "\n".join(problems)


def test_disposition_check_passes_and_fails_on_fixtures(tmp_path: Path) -> None:
    pattern = spec_register.FINDING_PATTERNS["ux-review-web.md"]
    doc = tmp_path / "ux-review-web.md"
    body = "# Review\n\n#### WUX-001 (high) a\n\n#### WUX-002 (low) b\n\n#### WUX-003 (low) c\n"
    doc.write_text(body, encoding="utf-8")
    assert spec_register.disposition_problems(doc, pattern) == ["ux-review-web.md: no '## Dispositions' section"] + [
        f"ux-review-web.md: WUX-00{n} has no disposition" for n in (1, 2, 3)
    ]

    doc.write_text(
        body + "\n## Dispositions\n\n| Finding | Disposition |\n| --- | --- |\n"
        "| WUX-001 | fixed (abc1234; tests/x.py) |\n| WUX-002 | no change (reason) |\n| WUX-003 | FIND-007 |\n",
        encoding="utf-8",
    )
    assert spec_register.disposition_problems(doc, pattern) == []

    doc.write_text(
        body + "\n## Dispositions\n\n| Finding | Disposition |\n| WUX-001 | fixed |\n| WUX-002 | later |\n",
        encoding="utf-8",
    )
    assert spec_register.disposition_problems(doc, pattern) == [
        "ux-review-web.md: WUX-002 disposition must start with fixed, no change or a register ID",
        "ux-review-web.md: WUX-003 has no disposition",
    ]


# TRACK-017: decision numbers -------------------------------------------------------------------

def test_decision_headings_are_unique_and_cited_decisions_exist() -> None:
    problems = spec_register.decision_problems(
        REGISTER.read_text(encoding="utf-8"), DECISIONS.read_text(encoding="utf-8")
    )
    assert not problems, "\n".join(problems)


def test_decision_check_passes_and_fails_on_fixtures() -> None:
    decisions = "## D1 One\n\n## D2 Two\n\n## D4 Four\n"
    assert spec_register.decision_problems("| A-001 | [w] | D1 and D4 |", decisions) == []
    assert spec_register.decision_problems("| A-001 | [w] | D3 |", decisions) == [
        "D3 is cited in the register but has no heading"
    ]
    assert spec_register.decision_problems("", decisions + "## D2 Again\n") == ["duplicate decision heading D2"]


# DOCS-002: AGENTS.md names every bridge module -------------------------------------------------

def unnamed_bridge_modules(agents: str, module_dir: Path) -> list[str]:
    return [
        path.stem
        for path in sorted(module_dir.glob("bridge*.py"))
        if not re.search(rf"\b{re.escape(path.stem)}\b", agents)
    ]


def test_agents_md_names_every_bridge_module() -> None:
    root = REGISTER.parents[2]
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert unnamed_bridge_modules(agents, root / "llm_tools") == []


def test_unnamed_bridge_module_is_detected(tmp_path: Path) -> None:
    (tmp_path / "bridge_a.py").write_text("", encoding="utf-8")
    (tmp_path / "bridge_ab.py").write_text("", encoding="utf-8")
    assert unnamed_bridge_modules("`bridge_a.py` is named", tmp_path) == ["bridge_ab"]
    assert unnamed_bridge_modules("`bridge_a` and bridge_ab", tmp_path) == []
