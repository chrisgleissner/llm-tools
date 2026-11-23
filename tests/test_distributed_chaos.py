"""Fault injection (PRD-008): one test per fault of ``scripts/bridge_chaos.py``, run against the real coordinator binary.

Each test starts a coordinator and real worker processes on 127.0.0.1, injects the fault and checks the outcome the fault
declares (``Fault.expected``): no silent loss, uncertain outcomes reported as uncertain, recovery without a person. The fast
tests check the registry, the report and the clock-shift library without a coordinator.
"""

from __future__ import annotations

import dataclasses
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
import dist_ws_proxy  # noqa: E402

PRD_FAULTS = {
    "coordinator-restart", "coordinator-kill9", "worker-partition", "disk-full", "clock-jump", "ws-slow-frames", "ws-dropped-frames",
    "db-corrupt-page",
}
EXTRA_FAULTS = {"slow-link"}  # DOG-019
DOC = ROOT / "docs" / "distributed" / "soak-and-chaos.md"


def test_every_prd_008_fault_is_registered_with_an_expected_outcome():
    ids = [fault.id for fault in bridge_chaos.FAULTS]
    assert set(ids) == PRD_FAULTS | EXTRA_FAULTS and len(ids) == len(set(ids))
    for fault in bridge_chaos.FAULTS:
        assert fault.title and len(fault.expected) > 80, fault.id
        assert fault.link in {"direct", "tcp", "frames", "slow"} and callable(fault.run)


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


def test_clock_jump_accepts_numeric_and_rfc3339_last_seen_values(monkeypatch):
    coordinator = type("Coordinator", (), {"api": type("Api", (), {"workers": staticmethod(lambda: [
        {"name": "numeric", "last_seen": 1_800_000_000},
        {"name": "rfc3339", "last_seen": "2026-11-08T04:09:10Z"},
        {"name": "missing", "last_seen": None},
    ])})})()
    fabric = type("Fabric", (), {"coordinator": coordinator})()
    assert bridge_chaos._worker_last_seen(fabric, "numeric") == 1_800_000_000
    assert bridge_chaos._worker_last_seen(fabric, "rfc3339") == 1_794_110_950
    assert bridge_chaos._worker_last_seen(fabric, "missing") == 0


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
def test_fault_meets_its_declared_outcome(fault, tmp_path, monkeypatch):
    binary = h.find_coordinator_binary()
    assert binary is not None
    if fault.id == "slow-link":
        if shutil.which("tmux") is None:
            pytest.skip("a remote terminal needs tmux")
        # The test size: long enough for two terminal opens and two large results; the CI job runs the default 180 s.
        monkeypatch.setattr(bridge_chaos, "SLOW_LINK", dataclasses.replace(bridge_chaos.SLOW_LINK, seconds=40.0, terminal_every=15.0, big_read_every=8.0))
    result = bridge_chaos.run_fault(fault, binary, tmp_path)
    if result.status == "skip":
        pytest.skip(result.reason)
    assert result.status == "pass", f"{fault.id}: {result.reason}\n{result.observed}"
    assert result.observed, "a passing fault reports what it observed"


class _GrowingNode:
    """A worker whose database gains events while the check runs: each read of the ids returns what exists at that moment."""

    def __init__(self, reads: list[list[int]]):
        self.reads = reads
        self.position = 0

    def event_ids(self, exclude_kinds: tuple[str, ...] = ()) -> list[int]:
        ids = self.reads[min(self.position, len(self.reads) - 1)]
        self.position += 1
        return list(ids)

    def last_event_id(self) -> int:
        return 51


class _FakeCoordinatorApi:
    def __init__(self, held: list[int]):
        self.held = held

    def workers(self) -> list[dict]:
        return [{"name": "chaos-w1", "event_cursor": 51}]

    def all_event_ids(self, _name: str) -> list[int]:
        return list(self.held)


def _fabric_with(node: _GrowingNode, coordinator_events: list[int]) -> bridge_chaos.Fabric:
    fabric = object.__new__(bridge_chaos.Fabric)
    fabric.nodes = {"chaos-w1": node}  # type: ignore[assignment]
    fabric.coordinator = type("Coordinator", (), {"api": _FakeCoordinatorApi(coordinator_events)})()  # type: ignore[assignment]
    return fabric


def test_events_written_and_replicated_while_the_check_runs_are_not_reported_as_invented():
    # The worker held 51 events when the worker was read first; it applied two more commands and the coordinator
    # received them before the coordinator was read (the db-corrupt-page failure under load: 51 vs 53).
    fabric = _fabric_with(_GrowingNode([list(range(1, 52)), list(range(1, 54))]), list(range(1, 54)))
    assert fabric.assert_events_replicated_exactly(timeout=1) == {"chaos-w1": 53}


def test_an_event_the_worker_never_wrote_is_still_reported_as_invented():
    fabric = _fabric_with(_GrowingNode([list(range(1, 52))]), [*range(1, 52), 99])
    with pytest.raises(bridge_chaos.Expectation, match=r"invented or duplicated \[99\]"):
        fabric.assert_events_replicated_exactly(timeout=1)


def test_an_acknowledged_event_missing_on_the_coordinator_is_still_reported_as_lost():
    fabric = _fabric_with(_GrowingNode([list(range(1, 52))]), [event for event in range(1, 52) if event != 7])
    with pytest.raises(bridge_chaos.Expectation, match=r"events lost \[7\]"):
        fabric.assert_events_replicated_exactly(timeout=1)


def test_a_repeated_event_on_the_coordinator_is_still_reported():
    fabric = _fabric_with(_GrowingNode([list(range(1, 52))]), [*range(1, 52), 51])
    with pytest.raises(bridge_chaos.Expectation, match="invented or duplicated"):
        fabric.assert_events_replicated_exactly(timeout=1)


# ---- slow-link (DOG-019) ----------------------------------------------------------------------------------

def _slow_link_observed(**changes):
    """What a healthy slow-link run measures; each test breaks one thing."""
    observed = {
        "reconnects": {"proxy_control_connections": 0, "coordinator_generation_increase": 0, "worker_log_connection_ended": 0, "worker_audit_connection_ended": 0},
        "terminals": {"opens": 3, "first_output_seconds": [1.4, 1.3, 1.5], "slowest": 1.5, "failed": []},
        "big_reads": {"count": 4, "slowest_seconds": 9.0, "result_bytes": 450000, "failed": []},
        "link": {"ping_round_trip": {"count": 100, "p50_ms": 321.0},
                 "bandwidth": {"up": {"peak_bps": 1.02e6, "busy_mean_bps": 0.96e6, "busy_seconds": 29.0}, "down": {"peak_bps": 8e3, "busy_mean_bps": 0.0, "busy_seconds": 0.0}}},
    }
    observed.update(changes)
    return observed


def _problems(**changes):
    return bridge_chaos.slow_link_problems(bridge_chaos.SlowLinkSettings(), dist_ws_proxy.LinkProfile(), _slow_link_observed(**changes))


def test_a_healthy_slow_link_run_has_no_problems():
    assert _problems() == []


def test_a_single_reconnect_seen_by_any_one_source_fails_the_case():
    for source in ("proxy_control_connections", "coordinator_generation_increase", "worker_log_connection_ended", "worker_audit_connection_ended"):
        reconnects = {**_slow_link_observed()["reconnects"], source: 1}
        problems = _problems(reconnects=reconnects)
        assert len(problems) == 1 and source in problems[0], source


def test_a_slow_or_missing_terminal_fails_the_case():
    slow = {"opens": 2, "first_output_seconds": [1.2, 3.4], "slowest": 3.4, "failed": []}
    assert "3.40s" in _problems(terminals=slow)[0]
    failed = {"opens": 2, "first_output_seconds": [1.2], "slowest": 1.2, "failed": [{"error": "no output within 30s"}]}
    assert "did not open" in _problems(terminals=failed)[0]
    assert "no terminal was opened" in _problems(terminals={"opens": 0, "first_output_seconds": [], "slowest": None, "failed": []})[0]


def test_a_large_result_that_never_arrives_fails_the_case_and_so_does_not_reading_one():
    assert "did not arrive" in _problems(big_reads={"count": 1, "failed": [{"state": "acked"}]})[0]
    assert "could not have been caught" in _problems(big_reads={"count": 0, "failed": []})[0]


def test_a_link_that_is_not_as_slow_or_as_busy_as_stated_fails_the_case():
    link = _slow_link_observed()["link"]
    fast = {**link, "ping_round_trip": {"count": 100, "p50_ms": 40.0}}
    assert "round trip 40 ms" in _problems(link=fast)[0]
    unknown = {**link, "ping_round_trip": {"count": 1}}
    assert "too few ping round trips" in _problems(link=unknown)[0]
    wide = {**link, "bandwidth": {**link["bandwidth"], "up": {"peak_bps": 5e6, "busy_mean_bps": 4e6, "busy_seconds": 29.0}}}
    assert "above the 1000000 bit/s limit" in _problems(link=wide)[0]
    idle = {**link, "bandwidth": {**link["bandwidth"], "up": {"peak_bps": 1e5, "busy_mean_bps": 0.0, "busy_seconds": 0.0}}}
    assert "never kept the link busy" in _problems(link=idle)[0]


def test_worker_reconnect_evidence_counts_the_workers_own_log_and_audit(tmp_path):
    node = type("Node", (), {})()
    node.log_path = tmp_path / "worker.log"
    node.state = tmp_path / "state"
    (node.state / "worker").mkdir(parents=True)
    node.log_path.write_text("Worker w connecting\nworker connection ended: TimeoutError: x\nworker connection ended: WebSocketClosed: y\n")
    (node.state / "worker" / "audit.jsonl").write_text('{"action": "worker.start"}\n{"action": "connection.ended"}\n')
    (node.state / "worker" / "audit.jsonl.1").write_text('{"action": "connection.ended"}\n')
    assert bridge_chaos._worker_reconnect_evidence(node) == {"worker_log_connection_ended": 2, "worker_audit_connection_ended": 2}
    node.log_path = tmp_path / "missing.log"
    assert bridge_chaos._worker_reconnect_evidence(node)["worker_log_connection_ended"] == 0


def test_slow_link_length_is_set_from_the_command_line(monkeypatch):
    settings = bridge_chaos.SlowLinkSettings()
    monkeypatch.setattr(bridge_chaos, "SLOW_LINK", settings)
    assert settings.seconds == 180.0  # the pull request size
    assert bridge_chaos.main(["--slow-link-seconds", "1800", "--fault", "nonsense"]) == 2
    assert settings.seconds == 1800.0
    fault = next(item for item in bridge_chaos.FAULTS if item.id == "slow-link")
    assert fault.link == "slow" and fault.workers == 1


def test_events_a_controller_never_replicates_are_not_reported_as_lost():
    # The worker holds ids 1..51; ids 10 and 20 are heartbeats that it never sends. The kinds are passed to the worker read.
    class Node(_GrowingNode):
        def event_ids(self, exclude_kinds=()):
            ids = super().event_ids()
            return [event for event in ids if not (exclude_kinds and event in (10, 20))]

    coordinator_events = [event for event in range(1, 52) if event not in (10, 20)]
    fabric = _fabric_with(Node([list(range(1, 52))]), coordinator_events)
    assert fabric.assert_events_replicated_exactly(timeout=1, unreplicated_kinds=("controller-heartbeat",)) == {"chaos-w1": 49}
    fabric = _fabric_with(Node([list(range(1, 52))]), coordinator_events)
    with pytest.raises(bridge_chaos.Expectation, match=r"events lost \[10, 20\]"):
        fabric.assert_events_replicated_exactly(timeout=1)
