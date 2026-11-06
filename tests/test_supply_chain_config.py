from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_external_workflow_actions_are_full_sha_pinned() -> None:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    uses_lines = [
        line.strip()
        for workflow in workflows
        for line in workflow.read_text().splitlines()
        if re.match(r"\s*uses:", line) and "./" not in line
    ]
    assert uses_lines
    unpinned = [line for line in uses_lines if not re.search(r"@[0-9a-f]{40}(?:\s|$)", line)]
    assert not unpinned, f"external actions must use immutable full commit SHAs: {unpinned}"


def test_supply_chain_policy_and_audit_jobs_are_wired() -> None:
    deny = (ROOT / "coordinator" / "deny.toml").read_text()
    assert 'multiple-versions = "deny"' in deny
    assert 'unknown-registry = "deny"' in deny
    assert 'unknown-git = "deny"' in deny
    assert "allow = [" in deny

    workflow = (ROOT / ".github" / "workflows" / "supply-chain.yml").read_text()
    assert "cargo audit --file coordinator/Cargo.lock" in workflow
    assert "cargo deny check advisories licenses bans sources)" in workflow
    assert "npm audit --omit=dev" in workflow
    assert "python -m pip_audit -r requirements-dev.txt" in workflow


def test_container_release_emits_sbom_signature_and_provenance() -> None:
    workflow = (ROOT / ".github" / "workflows" / "container-release.yml").read_text()
    assert '      - "*.*.*"' in workflow
    assert "container/build.sh --push" in workflow
    assert "cosign sign --yes" in workflow
    assert "actions/attest-build-provenance@" in workflow
    assert "cyclonedx-json=container.cdx.json" in workflow
    assert "coordinator-container-cyclonedx" in workflow


def test_release_workflow_tag_filter_matches_plain_semver_tags() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert '      - "*.*.*"' in workflow


def test_dependabot_covers_workflow_and_dependency_manifests() -> None:
    config = (ROOT / ".github" / "dependabot.yml").read_text()
    for ecosystem, directory in (("github-actions", "/"), ("cargo", "/coordinator"), ("npm", "/web"), ("pip", "/")):
        assert f"package-ecosystem: {ecosystem}" in config
        assert f"directory: {directory}" in config
