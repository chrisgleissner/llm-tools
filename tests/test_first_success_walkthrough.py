"""PRD-001: the scripted first-success walkthrough, timed, on a throw-away coordinator and worker."""
from __future__ import annotations

import json
import os
import re
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
    run.steps = [{"step": "init", "seconds": 0.6, "ok": True}, {"step": "serve", "seconds": 0.6, "ok": True}]
    run.cleanup = lambda: None  # type: ignore[method-assign]
    run.step = lambda name, action: None  # type: ignore[method-assign]
    report = run.run()
    assert not report["ok"] and "exceeds the 1s budget" in report["error"]
    assert walkthrough.render({"ok": False, "error": "x", "steps": [{"step": "init", "seconds": 1.0, "ok": False}], "total_seconds": 1.0, "budget_seconds": 300.0}).endswith("FAIL: x")


def test_a_failed_step_is_timed_and_reported(tmp_path, monkeypatch) -> None:
    run = walkthrough.Walkthrough(tmp_path / "none", tmp_path, budget=300.0)

    def fail() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(run.coordinator, "init", fail)
    run.cleanup = lambda: None  # type: ignore[method-assign]
    report = run.run()
    assert report["ok"] is False
    assert "boom" in report["error"]
    assert report["steps"][-1]["step"] == "init"
    assert report["steps"][-1]["ok"] is False
    assert report["steps"][-1]["seconds"] >= 0
    assert report["total_seconds"] == round(sum(step["seconds"] for step in report["steps"]), 2)


def test_wheel_file_accepts_a_wheel_or_a_directory_with_one(tmp_path) -> None:
    wheel = tmp_path / "a-1.0-py3-none-any.whl"
    wheel.touch()
    assert walkthrough.wheel_file(wheel) == wheel
    assert walkthrough.wheel_file(tmp_path) == wheel
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        walkthrough.wheel_file(empty)
    multiple = tmp_path / "multiple"
    multiple.mkdir()
    (multiple / "a.whl").touch()
    (multiple / "b.whl").touch()
    with pytest.raises(ValueError):
        walkthrough.wheel_file(multiple)


def test_container_coordinator_builds_the_docker_commands(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []

    def record(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(walkthrough.subprocess, "run", record)
    coordinator = walkthrough.ContainerCoordinator("img:tag", tmp_path)
    coordinator.init()
    coordinator.run("worker", "add", "w1")
    coordinator.stop()
    assert ["docker", "run", "--rm", "-v", f"{coordinator.volume}:/data", "img:tag", "init", "--hostname", "localhost"] in calls
    assert ["docker", "exec", coordinator.name, "/usr/local/bin/bridge-coordinator", "worker", "add", "w1"] in calls
    assert ["docker", "rm", "-f", coordinator.name] in calls
    assert ["docker", "volume", "rm", "-f", coordinator.volume] in calls


def jobs(text: str) -> dict[str, str]:
    parts = re.split(r"\n  ([A-Za-z0-9_-]+):\n", text[text.index("\njobs:\n") + 6:])
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_ci_runs_the_walkthrough_on_every_platform() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    job_bodies = jobs(workflow)
    for needed in ("--coordinator-image", "--wheel dist", "container/build.sh --load", "python -m build --wheel"):
        assert needed in job_bodies["first-success"], needed
    for needed in ("first_success_walkthrough.py", "--coordinator-only", "runner.os == 'Windows'", "runner.os == 'macOS'"):
        assert needed in job_bodies["cross-platform"], needed


def test_a_browser_spec_covers_the_console() -> None:
    spec = ROOT / "web" / "e2e" / "first-success.spec.ts"
    assert spec.exists()
    text = spec.read_text(encoding="utf-8")
    assert "toContainText" in text
    assert "bridge_soak_provider.py" in text
    assert '--mode", "background' in text


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
