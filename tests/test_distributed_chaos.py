"""Fault injection (PRD-008): one test per fault of ``scripts/bridge_chaos.py``, run against the real coordinator binary.

Each test starts a coordinator and real worker processes on 127.0.0.1, injects the fault and checks the outcome the fault
declares (``Fault.expected``): no silent loss, uncertain outcomes reported as uncertain, recovery without a person. The fast
tests check the registry, the report and the clock-shift library without a coordinator.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.distributed_harness import requires_coordinator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bridge_chaos  # noqa: E402
import bridge_soak  # noqa: E402
import dist_harness as h  # noqa: E402

PRD_FAULTS = {
    "coordinator-restart", "coordinator-kill9", "worker-partition", "disk-full", "clock-jump", "ws-slow-frames", "ws-dropped-frames",
    "db-corrupt-page",
}
DOC = ROOT / "docs" / "distributed" / "soak-and-chaos.md"


def test_every_prd_008_fault_is_registered_with_an_expected_outcome():
    ids = [fault.id for fault in bridge_chaos.FAULTS]
    assert set(ids) == PRD_FAULTS and len(ids) == len(set(ids))
    for fault in bridge_chaos.FAULTS:
        assert fault.title and len(fault.expected) > 80, fault.id
        assert fault.link in {"direct", "tcp", "frames"} and callable(fault.run)


def test_every_fault_and_phase_b_item_is_documented():
    text = DOC.read_text()
    for fault in bridge_chaos.FAULTS:
        assert f"`{fault.id}`" in text, f"{fault.id} is missing from {DOC.name}"
    for entry in bridge_chaos.PHASE_B:
        assert entry["id"] in text and entry["todo"].startswith("TODO(phase B)")
    for metric in h.DEFAULT_BOUNDS:
        assert metric in text, f"bound {metric} is missing from {DOC.name}"
    for entry in bridge_soak.PHASE_B:
        assert entry["id"] in text and entry["todo"].startswith("TODO(phase B)")


def test_list_prints_each_fault_and_its_expected_outcome(capsys):
    assert bridge_chaos.main(["--list"]) == 0
    output = capsys.readouterr().out
    assert all(fault.id in output and fault.expected in output for fault in bridge_chaos.FAULTS)
    assert "TODO(phase B)" in output


def test_unknown_fault_and_missing_binary_are_reported(monkeypatch, capsys):
    assert bridge_chaos.main(["--fault", "nonsense"]) == 2
    assert "unknown fault" in capsys.readouterr().err
    monkeypatch.setattr(h, "find_coordinator_binary", lambda *a, **k: None)
    assert bridge_chaos.main([]) == bridge_chaos.EXIT_NO_BINARY
    assert "cargo build" in capsys.readouterr().err


def test_a_skipped_fault_fails_the_run_unless_skips_are_allowed(monkeypatch, tmp_path):
    skipped = bridge_chaos.Fault("skipme", "t", "e" * 100, "direct", lambda fabric: {})

    def fake_run(fault, binary, base):
        return bridge_chaos.FaultResult(fault.id, fault.title, fault.expected, "skip", 0.0, reason="no compiler")

    monkeypatch.setattr(bridge_chaos, "FAULTS", [skipped])
    monkeypatch.setattr(bridge_chaos, "run_fault", fake_run)
    monkeypatch.setattr(h, "find_coordinator_binary", lambda *a, **k: Path("/bin/true"))
    report = tmp_path / "r.json"
    assert bridge_chaos.main(["--report", str(report)]) == bridge_chaos.EXIT_FAILED
    assert json.loads(report.read_text())["faults"][0]["status"] == "skip"
    assert bridge_chaos.main(["--report", str(report), "--allow-skips"]) == 0


def test_summary_marks_failures_and_lists_phase_b_todos():
    results = [bridge_chaos.FaultResult("a", "title a", "x", "pass", 1.0, {"k": 1}), bridge_chaos.FaultResult("b", "title b", "x", "fail", 2.0, reason="boom")]
    text = bridge_chaos.render_summary(results)
    assert "PASS  a" in text and "FAIL  b" in text and "boom" in text and "RESULT: FAIL" in text and "TODO  clock-jump-native" in text


def test_expect_raises_the_expectation_error_with_the_message():
    with pytest.raises(bridge_chaos.Expectation, match="nothing was lost"):
        bridge_chaos.expect(False, "nothing was lost")
    bridge_chaos.expect(True, "unused")


def test_page_search_finds_every_occurrence():
    assert bridge_chaos._occurrences(b"xxabxxabx", b"ab") == [2, 6]


@pytest.mark.skipif(shutil.which("cc") is None and shutil.which("gcc") is None, reason="a C compiler is required to build the clock shim")
def test_clock_shim_shifts_wall_time_forward_and_backward_while_the_process_runs(tmp_path):
    shim = bridge_chaos.build_shim(tmp_path)
    offset = tmp_path / "offset"
    script = ("import time,sys\n"
              "import ctypes\n"
              "libc = ctypes.CDLL(None)\n"
              "print(int(time.time()), flush=True)\n"
              "sys.stdin.readline()\n"
              "print(int(time.time()), flush=True)\n")
    env = {**h.base_environment(), "LD_PRELOAD": str(shim), "CHAOS_CLOCK_OFFSET_FILE": str(offset)}
    offset.write_text("0")
    for seconds in (3600, -7200):
        process = subprocess.Popen([sys.executable, "-c", script], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            before = int(process.stdout.readline())
            offset.write_text(str(seconds))
            import time

            time.sleep(0.1)
            process.stdin.write("go\n")
            process.stdin.flush()
            after = int(process.stdout.readline())
        finally:
            process.kill()
            process.wait()
            process.stdout.close()
            process.stdin.close()
        assert abs((after - before) - seconds) <= 2, (seconds, before, after)
        offset.write_text("0")


@requires_coordinator
@pytest.mark.parametrize("fault", bridge_chaos.FAULTS, ids=lambda fault: fault.id)
def test_fault_meets_its_declared_outcome(fault, tmp_path):
    binary = h.find_coordinator_binary()
    assert binary is not None
    result = bridge_chaos.run_fault(fault, binary, tmp_path)
    if result.status == "skip":
        pytest.skip(result.reason)
    assert result.status == "pass", f"{fault.id}: {result.reason}\n{result.observed}"
    assert result.observed, "a passing fault reports what it observed"
