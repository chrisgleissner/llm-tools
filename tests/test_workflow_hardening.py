"""CI workflows keep credentials, tokens and third-party code to the minimum each job needs."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))


def lines_of(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def test_workflows_exist() -> None:
    assert {path.name for path in FILES} >= {"pages.yml", "release.yml", "test.yml"}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_checkout_drops_the_token(path: Path) -> None:
    lines = lines_of(path)
    for index, line in enumerate(lines):
        if "uses: actions/checkout@" in line:
            window = "\n".join(lines[index + 1:index + 4])
            assert "persist-credentials: false" in window, f"{path.name}:{index + 1}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_third_party_actions_are_pinned_to_a_commit(path: Path) -> None:
    for number, line in enumerate(lines_of(path), 1):
        match = re.search(r"uses:\s*([\w.-]+)/([\w./-]+)@(\S+)", line)
        if not match or match.group(1) == "actions":
            continue
        assert re.fullmatch(r"[0-9a-f]{40}", match.group(3)), f"{path.name}:{number} must pin {match.group(1)}/{match.group(2)} to a commit SHA"
        assert re.search(r"#\s*v\d", line), f"{path.name}:{number} needs a version comment"


def _job_of(lines: list[str], index: int) -> str:
    for back in range(index, -1, -1):
        match = re.fullmatch(r"  ([A-Za-z0-9_-]+):\s*", lines[back])
        if match:
            return match.group(1)
    return ""


def _grants(path: Path, permission: str) -> list[tuple[str, int]]:
    lines = lines_of(path)
    start = next((i for i, line in enumerate(lines) if line.startswith("jobs:")), len(lines))
    found = []
    for index, line in enumerate(lines):
        if re.fullmatch(rf"\s*{permission}:\s*write\s*", line):
            found.append((_job_of(lines, index) if index > start else "<workflow>", index + 1))
    return found


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_workflow_wide_write_permission(path: Path) -> None:
    for permission in ("pages", "id-token", "contents"):
        assert not [grant for grant in _grants(path, permission) if grant[0] == "<workflow>"], f"{path.name}: {permission}: write at workflow level"


def test_oidc_and_pages_permissions_are_confined_to_one_job_each() -> None:
    assert _grants(WORKFLOWS / "pages.yml", "pages") == [("deploy", _grants(WORKFLOWS / "pages.yml", "pages")[0][1])]
    assert {job for job, _ in _grants(WORKFLOWS / "pages.yml", "id-token")} == {"deploy"}
    assert {job for job, _ in _grants(WORKFLOWS / "test.yml", "id-token")} == {"coverage-upload"}
    assert not _grants(WORKFLOWS / "release.yml", "id-token")


def test_the_job_that_runs_repository_tests_has_no_oidc_token() -> None:
    text = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
    test_job = text[text.index("\n  test:"):text.index("\n  coverage-upload:")]
    assert "id-token" not in test_job and "coverage run -m pytest" in test_job
    upload_job = text[text.index("\n  coverage-upload:"):]
    assert "coverage run" not in upload_job and "pip install" not in upload_job


def test_pages_artifact_excludes_repository_tooling() -> None:
    text = (WORKFLOWS / "pages.yml").read_text(encoding="utf-8")
    assert "path: docs/site" not in text
    for excluded in ("check_site.py", "README.md", "assets/src/"):
        assert f"--exclude '{excluded}'" in text
    assert "path: _site" in text
