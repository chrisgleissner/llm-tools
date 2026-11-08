from __future__ import annotations

import json
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
    assert "cargo audit --ignore RUSTSEC-2023-0071 --file coordinator/Cargo.lock" in workflow
    assert "cargo deny check advisories licenses bans sources)" in workflow
    assert "npm audit --omit=dev" in workflow
    assert "python -m pip_audit -r requirements-dev.txt" in workflow


def test_container_release_emits_sbom_signature_and_provenance() -> None:
    workflow = (ROOT / ".github" / "workflows" / "container-release.yml").read_text()
    assert '      - "*.*.*"' in workflow
    assert "container/build.sh --push" in workflow
    assert "cosign sign --yes \"$IMAGE@$DIGEST\"" in workflow
    assert "packages: write" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "actions/attest-build-provenance@" in workflow
    assert "subject-name: ${{ steps.build.outputs.image }}" in workflow
    assert "subject-digest: ${{ steps.build.outputs.digest }}" in workflow
    assert "push-to-registry: true" in workflow
    assert '"$RUNNER_TEMP/syft $IMAGE@$DIGEST -o cyclonedx-json=container.cdx.json"' in workflow
    assert "sha256sum --check" in workflow
    assert "coordinator-container-cyclonedx-${{ github.ref_name }}" in workflow
    assert "if-no-files-found: error" in workflow


def test_install_document_verifies_the_same_digest_and_expected_release_workflow() -> None:
    install = (ROOT / "docs" / "distributed" / "install.md").read_text()
    assert 'cosign verify "$IMAGE@sha256:DIGEST"' in install
    assert "container-release.yml@refs/tags/.*" in install
    assert "--certificate-oidc-issuer=https://token.actions.githubusercontent.com" in install
    assert 'gh attestation verify "oci://$IMAGE@sha256:DIGEST"' in install
    assert "--signer-workflow OWNER/REPO/.github/workflows/container-release.yml" in install
    assert "gh run download RUN_ID -n coordinator-container-cyclonedx-VERSION" in install
    assert "release job generates it from the immutable image digest" in install


def test_release_workflow_tag_filter_matches_plain_semver_tags() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert '      - "*.*.*"' in workflow


def test_dependabot_covers_workflow_and_dependency_manifests() -> None:
    config = (ROOT / ".github" / "dependabot.yml").read_text()
    for ecosystem, directory in (("github-actions", "/"), ("cargo", "/coordinator"), ("npm", "/web"), ("pip", "/")):
        assert f"package-ecosystem: {ecosystem}" in config
        assert f"directory: {directory}" in config


def test_dependabot_groups_every_ecosystem_into_one_pull_request() -> None:
    config = (ROOT / ".github" / "dependabot.yml").read_text()
    updates = config.split("  - package-ecosystem: ")[1:]
    assert len(updates) == 4
    for update in updates:
        ecosystem = update.splitlines()[0]
        assert "interval: weekly" in update, ecosystem
        assert "open-pull-requests-limit: 1" in update, ecosystem
        assert f"      {ecosystem}:\n        patterns: [\"*\"]" in update, ecosystem


def test_dynamic_security_scans_are_pinned_and_fail_on_medium_alerts() -> None:
    workflow = (ROOT / ".github" / "workflows" / "security-dynamic.yml").read_text()
    assert "schemathesis==4.33.1" in workflow
    assert "zaproxy/zap-stable:2.17.0" in workflow
    assert "st run docs/distributed/openapi.json" in workflow
    assert "--header \"Authorization: Bearer $SCAN_TOKEN\"" in workflow
    assert "--max-examples 10" in workflow
    assert "--max-failures 10" in workflow
    assert "--exclude-checks positive_data_acceptance" in workflow
    assert "--exclude-path-regex '^/api/v1/(stream|terminal|worker/connect|worker/terminal/.*)$'" in workflow
    assert "Fuzz read operations only" in workflow
    assert "--exclude-method POST --exclude-method PUT --exclude-method PATCH --exclude-method DELETE" in workflow
    assert "--workers 1" in workflow and "--max-time 120" in workflow
    assert "ulimit -v 4194304" in workflow and "timeout --signal=TERM --kill-after=15s 180s" in workflow
    assert "Schemathesis exceeded its 180-second / 4-GiB resource budget" in workflow
    assert "Coordinator stopped responding during Schemathesis" in workflow
    assert "zap-api-scan.py" in workflow and "-f openapi" in workflow
    assert "zap-baseline.py" in workflow
    assert workflow.count("insights.exitAuto=false") == 2
    assert workflow.count("-e NO_PROXY=localhost,127.0.0.1") == 2
    assert workflow.count("-e no_proxy=localhost,127.0.0.1") == 2
    assert "insights.exitAutoOnHigh" not in workflow
    assert "alert report is evaluated by our gate" in workflow
    assert "scripts/check_zap_reports.py" in workflow
    assert "if risk >= 2:" in (ROOT / "scripts" / "check_zap_reports.py").read_text()
    assert "dynamic-security-reports" in workflow
    assert 'chmod 0777 "$RUNNER_TEMP/zap-reports"' in workflow
    assert workflow.count('"$RUNNER_TEMP/zap-reports:/zap/wrk/:rw"') == 2
    assert 'python scripts/check_zap_reports.py "$RUNNER_TEMP/zap-reports"' in workflow
    for report in ("zap-api-report.json", "zap-api-report.html", "zap-console-report.json", "zap-console-report.html"):
        assert f"-J {report}" in workflow or f"-r {report}" in workflow
    assert "/zap/wrk/zap-console-report" not in workflow
    assert "4f17be931a9d60f3f987975aea8221f1a8eaf7aa" in workflow
    assert "--severity MEDIUM" in workflow
    assert 'severity in {"MEDIUM", "HIGH", "CRITICAL"}' in workflow
    assert "SERVER_CERT_VALID_DAYS: i64 = 365" in (ROOT / "coordinator" / "src" / "certs.rs").read_text()
    assert "internal_server_certificate_validity_stays_below_398_days" in (
        ROOT / "coordinator" / "tests" / "certs.rs"
    ).read_text()
    assert "openssl ca -batch -config \"$pki/openssl.cnf\" -gencrl" in workflow
    assert "crlDistributionPoints = URI:http://127.0.0.1:18444/ca.crl" in workflow
    assert "tls_cert_file =" in workflow and "tls_key_file =" in workflow
    assert 'python -m http.server 18444 --bind 127.0.0.1 --directory "$pki"' in workflow
    assert 'if [ "$crl_server_ready" -ne 1 ]; then' in workflow
    assert "The local CA-signed CRL endpoint did not become ready" in workflow
    assert 'mutation_limit_per_token = 10000' in workflow
    assert 'mutation_limit_per_address = 10000' in workflow
    assert 'mutation_limit_global = 10000' in workflow


def test_openapi_json_depth_schema_stays_within_scanner_budget() -> None:
    document = json.loads((ROOT / "docs" / "distributed" / "openapi.json").read_text())
    schemas = document["components"]["schemas"]
    depth_names = sorted(
        (name for name in schemas if name.startswith("BoundedJsonValueDepth")),
        key=lambda name: int(name.removeprefix("BoundedJsonValueDepth")),
    )
    assert depth_names == [f"BoundedJsonValueDepth{depth}" for depth in range(5)]
    deepest = schemas["BoundedJsonValueDepth4"]["anyOf"]
    assert len(deepest) == 5
    assert "4 nested levels" in schemas["BoundedRequestObject"]["description"]
