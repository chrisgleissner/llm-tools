"""The cargo audit and cargo deny advisory exceptions must stay identical (PRD-025)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cargo_audit_ignores_exactly_the_advisories_cargo_deny_accepts() -> None:
    deny = tomllib.loads((ROOT / "coordinator" / "deny.toml").read_text())
    deny_ids = {entry["id"] if isinstance(entry, dict) else entry for entry in deny["advisories"].get("ignore", [])}
    audit = tomllib.loads((ROOT / "coordinator" / ".cargo" / "audit.toml").read_text())
    audit_ids = set(audit["advisories"].get("ignore", []))
    assert audit_ids == deny_ids
    assert all(re.fullmatch(r"RUSTSEC-\d{4}-\d{4}", item) for item in audit_ids)
