"""PRD-001: the scripted first-success walkthrough, timed, on a throw-away coordinator and worker."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "first_success_walkthrough.py"
sys.path.insert(0, str(ROOT / "scripts"))

import first_success_walkthrough as walkthrough  # noqa: E402


def test_the_budget_is_the_prd_limit_of_five_minutes() -> None:
    assert walkthrough.DEFAULT_BUDGET_SECONDS == 300


def test_report_fails_when_the_total_exceeds_the_budget(tmp_path) -> None:
    run = walkthrough.Walkthrough(tmp_path / "none", tmp_path, budget=1.0)
    run.steps = [("init", 0.6), ("serve", 0.6)]
    run.cleanup = lambda: None  # type: ignore[method-assign]
    run.step = lambda name, action: None  # type: ignore[method-assign]
    report = run.run()
    assert not report["ok"] and "exceeds the 1s budget" in report["error"]
    assert walkthrough.render({"ok": False, "error": "x", "steps": [{"step": "init", "seconds": 1.0}], "total_seconds": 1.0, "budget_seconds": 300.0}).endswith("FAIL: x")


def test_missing_binary_exits_with_status_2(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BRIDGE_COORDINATOR_BIN", raising=False)
    done = subprocess.run([sys.executable, str(SCRIPT), "--coordinator-bin", str(tmp_path / "missing")], capture_output=True, text=True)
    assert done.returncode == 2 and "bridge-coordinator" in done.stderr


def test_walkthrough_reaches_a_visible_mission_within_the_budget() -> None:
    binary = os.environ.get("BRIDGE_COORDINATOR_BIN") or next((str(p) for p in (ROOT / "coordinator" / "target" / profile / "bridge-coordinator" for profile in ("release", "debug")) if p.exists()), "")
    if not binary or not Path(binary).exists():
        if os.environ.get("BRIDGE_REQUIRE_COORDINATOR") == "1":
            pytest.fail("BRIDGE_REQUIRE_COORDINATOR=1 but no bridge-coordinator binary")
        pytest.skip("no bridge-coordinator binary")
    if sys.platform.startswith("win"):
        pytest.skip("the walkthrough harness is Linux and macOS only")
    done = subprocess.run([sys.executable, str(SCRIPT), "--coordinator-bin", binary, "--json"], capture_output=True, text=True, timeout=400)
    report = json.loads(done.stdout)
    assert done.returncode == 0 and report["ok"], report
    assert [step["step"] for step in report["steps"]] == ["init", "serve", "token", "enroll", "connect", "mission", "visible"]
    assert report["total_seconds"] <= 300
