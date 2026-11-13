"""Release workflow structure (PRD-002): a dry run on pull requests, publishing only on tags."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CONTAINER_WORKFLOW = ROOT / ".github" / "workflows" / "container-release.yml"


def jobs(text: str) -> dict[str, str]:
    parts = re.split(r"\n  ([A-Za-z0-9_-]+):\n", text[text.index("\njobs:\n") + 6:])
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_the_workflow_runs_on_tags_and_on_pull_requests() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    header = text[:text.index("\njobs:")]
    assert re.search(r"^  push:\n    tags:\n", header, re.M)
    assert re.search(r"^  pull_request:\n", header, re.M)
    assert "secrets." not in text, "the dry run and release jobs must not need repository secrets"


def test_the_dry_run_builds_every_artifact_and_pushes_nothing() -> None:
    body = jobs(WORKFLOW.read_text(encoding="utf-8"))["dry-run"]
    assert "if: github.event_name == 'pull_request'" in body
    for needed in ("python -m build", "container/build.sh --oci", "for arch in amd64 arm64", "bridge-coordinator-linux-$arch",
                   "release_checksums.py", "sha256sum --check"):
        assert needed in body, needed
    for forbidden in ("--push", "docker/login-action", "cosign sign", "id-token", "packages: write", "contents: write", "gh-release"):
        assert forbidden not in body, forbidden


def test_publishing_is_tag_only() -> None:
    body = jobs(WORKFLOW.read_text(encoding="utf-8"))["release"]
    assert re.search(r"^    if: github.ref_type == 'tag'$", body, re.M)
    publish = body[body.index("Publish GitHub release"):]
    assert "if: github.ref_type == 'tag'" in publish and "SHA256SUMS" in publish


def test_the_container_workflow_signs_and_pushes_only_for_tags() -> None:
    text = CONTAINER_WORKFLOW.read_text(encoding="utf-8")
    header = text[:text.index("\njobs:")]
    assert "tags:" in header and "pull_request" not in header


def test_release_checksums_script_writes_a_verifiable_file(tmp_path) -> None:
    (tmp_path / "b.whl").write_bytes(b"wheel")
    (tmp_path / "a.tar.gz").write_bytes(b"sdist")
    output = tmp_path / "SHA256SUMS"
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "release_checksums.py"), str(tmp_path), str(output)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert output.read_text().splitlines() == [f"{hashlib.sha256(b'sdist').hexdigest()}  a.tar.gz", f"{hashlib.sha256(b'wheel').hexdigest()}  b.whl"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert subprocess.run([sys.executable, str(ROOT / "scripts" / "release_checksums.py"), str(empty), str(empty / "S")], capture_output=True).returncode == 1
