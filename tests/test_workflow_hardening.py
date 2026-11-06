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
    start = text.index("\n  coverage-upload:")
    following = re.search(r"\n  [a-z0-9-]+:\n", text[start + 1:])
    upload_job = text[start:start + 1 + following.start()] if following else text[start:]
    assert "coverage run" not in upload_job and "pip install" not in upload_job


def test_pages_artifact_excludes_repository_tooling() -> None:
    text = (WORKFLOWS / "pages.yml").read_text(encoding="utf-8")
    assert "path: docs/site" not in text
    for excluded in ("check_site.py", "README.md", "assets/src/"):
        assert f"--exclude '{excluded}'" in text
    assert "path: _site" in text


def _jobs(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"\n  ([A-Za-z0-9_-]+):\n", text[text.index("\njobs:\n") + 6:])
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)} | ({parts[-2]: parts[-1]} if len(parts) > 2 else {})


def test_every_test_job_has_a_timeout_and_matrices_do_not_fail_fast() -> None:
    for name, body in _jobs(WORKFLOWS / "test.yml").items():
        assert re.search(r"^    timeout-minutes: \d+$", body, re.M), f"job {name} needs timeout-minutes"
        if "matrix:" in body:
            assert "fail-fast: false" in body, f"job {name} must set fail-fast: false"


def test_the_platform_matrix_covers_six_hosted_platforms() -> None:
    text = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
    for label in ("ubuntu-latest", "ubuntu-24.04-arm", "macos-latest", "macos-15-intel", "windows-latest", "windows-11-arm"):
        assert label in text, label
    jobs = _jobs(WORKFLOWS / "test.yml")
    assert "cargo clippy --locked --all-targets -- -D warnings" in jobs["cross-platform"]
    assert "BRIDGE_COORDINATOR_BIN" in jobs["cross-platform"] and "BRIDGE_REQUIRE_COORDINATOR" in jobs["cross-platform"]
    assert "chromium" in jobs["web-cross"] and "firefox" in jobs["web-cross"]


def test_test_workflow_runs_branches_through_pull_requests_with_one_run_per_ref() -> None:
    text = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
    header = text[:text.index("\njobs:")]
    assert "  push:\n    branches: [main]\n  pull_request:\n" in header
    assert "group: test-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}" in header
    assert "cancel-in-progress: true" in header


def _windows_tests() -> list[str]:
    job = _jobs(WORKFLOWS / "test.yml")["cross-platform"]
    block = job[job.index("WINDOWS_TESTS: >-") :].splitlines()[1:]
    entries: list[str] = []
    for line in block:
        if not line.startswith("            ") or not line.strip().startswith("tests/"):
            break
        entries.append(line.strip())
    return entries


def test_windows_jobs_run_an_allowlist_whose_entries_all_exist() -> None:
    """The Windows step runs WINDOWS_TESTS only; every entry must name an existing file and test."""
    entries = _windows_tests()
    assert "tests/test_windows_policy.py" in entries and "tests/test_distributed_integration.py" in entries
    root = WORKFLOWS.parents[1]
    for entry in entries:
        file, _, name = entry.partition("::")
        assert (root / file).is_file(), entry
        if name:
            assert re.search(rf"^def {re.escape(name)}\(", (root / file).read_text(encoding="utf-8"), re.M), entry
    job = _jobs(WORKFLOWS / "test.yml")["cross-platform"]
    assert "python -m pytest -q $WINDOWS_TESTS" in job and "if: runner.os == 'Windows'" in job


def test_every_job_that_runs_cargo_installs_the_toolchain_through_the_shared_action() -> None:
    """One shared action installs rust-toolchain.toml; no job carries its own rustup commands."""
    action = (WORKFLOWS.parent / "actions" / "rust-toolchain" / "action.yml").read_text(encoding="utf-8")
    assert "rustup toolchain uninstall" in action and "rustup toolchain install\n" in action
    for path in FILES:
        for name, job in _jobs(path).items():
            uses_rust = re.search(r"\bcargo |container/build\.sh|rust-cache@", job)
            if uses_rust:
                assert "uses: ./.github/actions/rust-toolchain" in job, f"{path.name}: {name}"
            assert "rustup toolchain" not in job and "rustup-init" not in job, f"{path.name}: {name}"
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"- uses: Swatinem/rust-cache@", text):
            previous = text[: match.start()].rstrip().splitlines()[-1]
            assert previous.strip() == "- uses: ./.github/actions/rust-toolchain", f"{path.name}: toolchain must come before rust-cache"
