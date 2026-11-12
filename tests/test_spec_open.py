"""``scripts/spec_open.py`` lists the register IDs that are open for a release."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "spec_open.py"

REGISTER = """| ID | Status | Evidence |
| --- | --- | --- |
| AAA-001 | [x] | done |
| AAA-002 | [~] | partly |
| BBB-001 | [d] | D1 target 1.6.0 |
| BBB-002 | [w] | D1 |
| PROV-001 | [ ] | |
| LB-004 | [ ] | |
"""


def run(tmp_path: Path, *args: str, text: str = REGISTER) -> subprocess.CompletedProcess[str]:
    register = tmp_path / "SPEC_STATUS.md"
    register.write_text(text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--register", str(register), *args], capture_output=True, text=True, check=False
    )


def test_release_1_5_0_ignores_later_rows_and_closes_earlier_deferrals(tmp_path: Path) -> None:
    result = run(tmp_path, "--release", "1.5.0")
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["AAA: AAA-002", "1 open for release 1.5.0"]


def test_release_1_6_0_checks_all_rows_and_reopens_the_deferral(tmp_path: Path) -> None:
    result = run(tmp_path, "--release", "1.6.0", "--json")
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "release": "1.6.0",
        "count": 4,
        "open": {"AAA": ["AAA-002"], "BBB": ["BBB-001"], "PROV": ["PROV-001"], "LB": ["LB-004"]},
    }


def test_exit_zero_when_nothing_is_open(tmp_path: Path) -> None:
    result = run(tmp_path, "--release", "1.5.0", text="| AAA-001 | [x] | done |\n| BBB-001 | [d] | D1 target 1.6.0 |\n")
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["0 open for release 1.5.0"]


def test_runs_against_the_real_register() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--release", "1.5.0"], capture_output=True, text=True, check=False
    )
    assert result.returncode in (0, 1)
    assert result.stdout.splitlines()[-1].endswith("open for release 1.5.0")
