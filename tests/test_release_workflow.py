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
    text = WORKFLOW.read_text(encoding="utf-8")
    header = text[:text.index("\njobs:")]
    body = jobs(text)["dry-run"]
    assert re.search(r"^    if: github.event_name == 'pull_request'$", body, re.M)
    for needed in ("python -m build", "container/build.sh --push", "install-syft", "cosign generate-key-pair",
                   "cosign sign", "cosign verify ", "cosign attest", "cosign verify-attestation",
                   "release_checksums.py", "sha256sum --check"):
        assert needed in body, needed
    for line in body.splitlines():
        if "--push" in line:
            assert "REGISTRY" in line or "localhost:5000" in line, line
        if re.match(r"^\s*cosign (?:sign|attest|verify)\b", line):
            assert "--key" in line, line
    for forbidden in ("docker/login-action", "id-token", "packages: write", "contents: write",
                      "attestations: write", "secrets.", "ghcr.io", "softprops/action-gh-release"):
        assert forbidden not in body, forbidden
    pull_request = re.search(r"^  pull_request:\n(?P<body>(?:^(?!  [A-Za-z_][A-Za-z0-9_-]*:).*(?:\n|$))*)", header, re.M)
    assert pull_request, "workflow must run on pull requests"
    assert "paths:" not in pull_request.group("body"), "dry run must run on every pull request"


def test_publishing_is_tag_only() -> None:
    body = jobs(WORKFLOW.read_text(encoding="utf-8"))["release"]
    assert re.search(r"^    if: github.ref_type == 'tag'$", body, re.M)
    publish = body[body.index("Publish GitHub release"):]
    assert "if: github.ref_type == 'tag'" in publish and "SHA256SUMS" in publish


def test_the_container_workflow_signs_and_pushes_only_for_tags() -> None:
    text = CONTAINER_WORKFLOW.read_text(encoding="utf-8")
    header = text[:text.index("\njobs:")]
    assert "tags:" in header and "pull_request" not in header
    body = jobs(text)["publish"]
    assert re.match(r"^    if: github.ref_type == 'tag'\n", body)
    assert "id-token: write" in body
    signing = next(line for line in body.splitlines() if "cosign sign --yes \"$IMAGE@$DIGEST\"" in line)
    assert "cosign sign --yes \"$IMAGE@$DIGEST\"" in signing
    assert "--key" not in signing
    assert "attest-build-provenance" in body
    assert "install-syft" in body


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
