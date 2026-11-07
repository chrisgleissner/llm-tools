"""Repository-level release hygiene checks (PRD-024, PRD-025)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_security_policy_names_reporting_channel_and_response_times() -> None:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "/security/advisories/new" in text
    for heading in ("## Reporting a vulnerability", "## Supported versions", "## Dependencies and advisories"):
        assert heading in text
    assert re.search(r"Acknowledge the report \| \d+ working days", text)


def test_dependabot_covers_every_shipped_ecosystem() -> None:
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    entries = re.findall(r"package-ecosystem: (\S+)\n\s+directory: (\S+)", text)
    assert set(entries) == {("cargo", "/coordinator"), ("npm", "/web"), ("github-actions", "/"), ("pip", "/")}
    for directory in ("coordinator/Cargo.lock", "web/package-lock.json", "pyproject.toml"):
        assert (ROOT / directory).exists()


def test_audit_workflow_runs_weekly_for_all_lock_files() -> None:
    text = (ROOT / ".github" / "workflows" / "audit.yml").read_text(encoding="utf-8")
    assert "schedule:" in text and "cron:" in text
    for command in ("cargo audit", "npm audit", "pip-audit"):
        assert command in text
