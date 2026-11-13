"""The soak harness (PRD-007): its statistics, exactly-once verification, fake provider, frame proxy and a short end-to-end run.

The fast tests use no coordinator. They show that each pass criterion of ``scripts/bridge_soak.py`` can fail:
a flat series passes and a leaking one fails the slope check, a missing, repeated or invented effect is
reported by ``verify_exactly_once``, and a slow request class fails the latency budget. The end-to-end test at the
end runs the real binary, real worker processes and the fake provider for a short time.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge_ws
from llm_tools.bridge_store import BridgeStore
from tests.conftest import scaled
from tests.distributed_harness import requires_coordinator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bridge_soak  # noqa: E402
import dist_harness as h  # noqa: E402
import dist_ws_proxy  # noqa: E402

PROVIDER = ROOT / "scripts" / "bridge_soak_provider.py"


# ---- statistics -----------------------------------------------------------------------------

def test_percentile_is_nearest_rank_and_empty_safe():
    values = [float(n) for n in range(1, 101)]
    assert h.percentile(values, 0.5) == 50.0
    assert h.percentile(values, 0.99) == 99.0
    assert h.percentile(values, 1.0) == 100.0
    assert h.percentile([], 0.99) == 0.0
    assert h.percentile([7.0], 0.99) == 7.0


def test_least_squares_recovers_slope_and_intercept():
    slope, intercept = h.least_squares([(t, 3.0 * t + 5.0) for t in range(10)])
    assert slope == pytest.approx(3.0) and intercept == pytest.approx(5.0)
    assert h.least_squares([(1.0, 4.0)]) == (0.0, 4.0)
    assert h.least_squares([(2.0, 1.0), (2.0, 9.0)])[0] == 0.0  # no spread in time


BOUND = h.SlopeBound(per_hour=100.0, floor=50.0, unit="units")


def _series(rate_per_hour: float, *, samples: int = 40, step: float = 60.0, noise: float = 0.0, seed: int = 1) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    return [(index * step, 1000.0 + rate_per_hour / 3600.0 * index * step + rng.uniform(-noise, noise)) for index in range(samples)]


def test_flat_series_with_noise_passes_the_slope_check():
    result = h.check_slope("m", _series(0.0, noise=20.0), BOUND)
    assert result.verdict == "pass", result.detail


def test_leaking_series_fails_the_slope_check():
    result = h.check_slope("m", _series(900.0, noise=5.0), BOUND)
    assert result.verdict == "fail" and result.slope_per_hour == pytest.approx(900.0, rel=0.05), result.detail


def test_growth_above_the_bound_but_below_the_floor_is_noise():
    short = _series(6000.0, samples=6, step=1.0)  # six seconds: fitted rise is about 10 units, floor is 50
    assert h.check_slope("m", short, BOUND, min_samples=4).verdict == "pass"


def test_warmup_ramp_is_excluded_from_the_fit():
    ramp = [(index * 60.0, 5000.0 - 400.0 * max(0, 10 - index)) for index in range(40)]  # grows for the first quarter, then flat
    assert h.check_slope("m", ramp, BOUND, warmup_fraction=0.3).verdict == "pass"
    assert h.check_slope("m", ramp, BOUND, warmup_fraction=0.0).verdict == "fail"


def test_stated_warmup_window_is_ignored_in_seconds_not_as_a_share_of_the_run():
    # 20 minutes of ramp, then flat for 40 minutes: only a warm-up in seconds can cut exactly at the knee
    ramp = [(index * 60.0, 5000.0 - 400.0 * max(0, 20 - index)) for index in range(60)]
    assert h.check_slope("m", ramp, BOUND, warmup_seconds=20 * 60.0).verdict == "pass"
    assert h.check_slope("m", ramp, BOUND, warmup_seconds=10 * 60.0).verdict == "fail"
    # the fraction is used only when no number of seconds is given
    assert h.check_slope("m", ramp, BOUND, warmup_fraction=0.4).verdict == "pass"


def test_ceiling_fails_on_the_peak_even_when_the_slope_is_flat():
    assert h.check_ceiling("m", [(0.0, 1000.0), (60.0, 1400.0)], 1500.0, "units").verdict == "pass"
    over = h.check_ceiling("m", [(0.0, 1000.0), (60.0, 1600.0), (120.0, 1000.0)], 1500.0, "units")
    assert over.verdict == "fail" and "1600" in over.detail and over.metric == "m ceiling"
    assert h.check_ceiling("m", [], 1500.0, "units").verdict == "inconclusive"


def test_resources_include_a_ceiling_per_process_and_it_sees_one_large_process():
    def sample(t, largest):
        return {"t": t, "coordinator": {"rss_kib": 1000}, "workers": {"rss_kib": 3000, "max_rss_kib": 1000}, "controllers": {"rss_kib": 3000, "max_rss_kib": largest}}

    samples = [sample(index * 30.0, 1000 if index != 5 else 70_000) for index in range(10)]
    results = {item.metric: item for item in h.evaluate_resources(samples, {}, ceilings={"controllers_max_rss_kib": (64 * 1024, "KiB")})}
    assert results["controllers_max_rss_kib ceiling"].verdict == "fail"
    assert {"controllers_max_rss_kib", "workers_max_rss_kib", "coordinator_rss_kib"} == set(h.DEFAULT_CEILINGS)
    assert [h.metric_value(samples[0], name) for name in ("controllers_max_rss_kib", "workers_max_rss_kib")] == [1000.0, 1000.0]


def test_too_few_samples_is_inconclusive_not_a_silent_pass():
    assert h.check_slope("m", _series(0.0, samples=4), BOUND, min_samples=4).verdict == "inconclusive"
    assert h.check_slope("m", [], BOUND).verdict == "inconclusive"


def test_database_must_be_explained_by_its_rows():
    ok = [{"t": 1.0, "db_bytes": h.DB_BASE_BYTES + 100 * h.DB_BYTES_PER_ROW, "rows": {"events": 60, "commands": 40}}]
    bad = [{"t": 2.0, "db_bytes": 10 * h.DB_BASE_BYTES, "rows": {"events": 10}}]
    assert h.check_database_explained_by_rows(ok) == []
    assert "t=2s" in h.check_database_explained_by_rows(bad)[0]


def test_latency_budget_fails_on_the_p99_not_the_median():
    recorder = h.LatencyRecorder()
    for _ in range(98):
        recorder.add("read", 0.002)
    recorder.add("read", 0.9)
    recorder.add("read", 0.9)
    recorder.error("read")
    results = {item["class"]: item for item in h.check_latency(recorder, {"read": 250.0, "submit": 500.0})}
    assert results["read"]["verdict"] == "fail" and results["read"]["p50_ms"] == 2.0 and results["read"]["errors"] == 1
    assert results["submit"]["verdict"] == "pass" and results["submit"]["count"] == 0


def test_metric_names_of_the_bounds_are_all_sampled():
    sample = {"coordinator": {"rss_kib": 1, "fds": 2, "threads": 3}, "db_bytes": 4, "workers": {"rss_kib": 5, "fds": 6, "threads": 7},
              "controllers": {"rss_kib": 8, "fds": 9}}
    assert [h.metric_value(sample, metric) for metric in h.DEFAULT_BOUNDS] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]


def test_per_process_bounds_scale_with_the_fleet_and_totals_do_not():
    bounds = h.scale_bounds(h.DEFAULT_BOUNDS, workers=10, controllers=50)
    assert bounds["workers_rss_kib"].per_hour == 10 * h.DEFAULT_BOUNDS["workers_rss_kib"].per_hour
    assert bounds["controllers_rss_kib"].floor == 50 * h.DEFAULT_BOUNDS["controllers_rss_kib"].floor
    assert bounds["coordinator_rss_kib"] == h.DEFAULT_BOUNDS["coordinator_rss_kib"]


def test_proc_stats_reads_this_process_and_none_for_a_dead_one():
    stats = h.proc_stats(os.getpid())
    assert stats and stats["rss_kib"] > 0 and stats["fds"] >= 3 and stats["threads"] >= 1
    assert h.proc_stats(2**22 + 12345) is None and h.proc_stats(None) is None


# ---- arguments -------------------------------------------------------------------------------

def test_soak_defaults_are_the_prd_scaled_run():
    args = bridge_soak.parse_args([])
    assert (args.workers, args.missions, args.duration, args.sample_seconds) == (10, 50, 2700.0, 60.0)
    assert bridge_soak.effective_warmup(args.duration, args.warmup_fraction, args.warmup_seconds) == 1800.0  # a default run keeps a 15-minute fit
    assert bridge_soak.validate(args) is None


def test_count_floor_multiplier_scales_only_the_descriptor_and_thread_floors():
    default = bridge_soak.chosen_bounds(bridge_soak.parse_args([]))
    assert all(default[metric].floor == bound.floor for metric, bound in h.DEFAULT_BOUNDS.items())
    scaled_args = bridge_soak.parse_args(["--count-floor-multiplier", "8", "--bound-coordinator-threads", "5"])
    scaled = bridge_soak.chosen_bounds(scaled_args)
    for metric, bound in h.DEFAULT_BOUNDS.items():
        counted = metric.endswith(("_fds", "_threads"))
        assert scaled[metric].floor == bound.floor * (8 if counted else 1), metric
    assert scaled["coordinator_threads"].per_hour == 5.0 and scaled["coordinator_rss_kib"].per_hour == h.DEFAULT_BOUNDS["coordinator_rss_kib"].per_hour
    assert "at least 1" in bridge_soak.validate(bridge_soak.parse_args(["--count-floor-multiplier", "0.5"]))


def test_effective_warmup_is_the_stated_time_capped_at_two_thirds_of_the_run():
    assert bridge_soak.effective_warmup(2700.0, 0.25, 1200.0) == 1200.0
    assert bridge_soak.effective_warmup(1800.0, 0.25, 1200.0) == 1200.0  # the 30 minute run keeps 10 minutes
    assert bridge_soak.effective_warmup(900.0, 0.25, 1200.0) == 600.0
    assert bridge_soak.effective_warmup(300.0, 0.25, 1200.0) == 200.0  # the pull request run
    assert bridge_soak.effective_warmup(300.0, 0.8, 1200.0) == 240.0  # the fraction is a floor


def test_soak_refuses_a_configuration_without_enough_samples():
    args = bridge_soak.parse_args(["--duration", "60", "--sample-seconds", "60"])
    assert "samples after warm-up" in bridge_soak.validate(args)
    assert "at least --workers" in bridge_soak.validate(bridge_soak.parse_args(["--workers", "5", "--missions", "3"]))


def test_soak_without_a_coordinator_binary_exits_with_a_clear_status(monkeypatch, capsys):
    monkeypatch.setattr(h, "find_coordinator_binary", lambda *a, **k: None)
    assert bridge_soak.main(["--duration", "300", "--sample-seconds", "15"]) == bridge_soak.EXIT_NO_BINARY
    assert "cargo build" in capsys.readouterr().err


# ---- exactly-once verification -----------------------------------------------------------------

class FakeApi:
    def __init__(self, entries: dict[str, dict[str, Any]]) -> None:
        self.entries = entries

    def get_command(self, request_id: str) -> tuple[int, dict[str, Any]]:
        entry = self.entries.get(request_id)
        return (200, entry) if entry is not None else (404, {})


class FakeNode:
    def __init__(self, markers: dict[str, int], commands: dict[str, list[str]]) -> None:
        self.markers, self.commands = markers, commands

    def marker_count(self, marker: str) -> int:
        return self.markers.get(marker, 0)

    def command_status(self, request_id: str) -> list[str]:
        return self.commands.get(request_id, [])


def _done(outcome: str) -> dict[str, Any]:
    return {"state": "done", "result": {"outcome": outcome}}


def _scenario(outcome: str | None, *, effects: int, rows: int = 1, accepted: bool = True) -> h.Verification:
    ledger = h.Ledger("t")
    issued, _ = ledger.add_task_request("w", "m1")
    issued.accepted = accepted
    entries = {} if outcome is None else {issued.request_id: _done(outcome)}
    if outcome is None and accepted:
        entries[issued.request_id] = {"state": "sent", "result": None}
    node = FakeNode({issued.marker: effects} if effects else {}, {issued.request_id: ["APPLIED"] * rows} if rows else {})
    return h.verify_exactly_once(ledger, FakeApi(entries), {"w": node}, settle_seconds=0.0)  # type: ignore[arg-type]


def test_verify_accepts_one_effect_for_an_applied_command():
    report = _scenario("applied", effects=1)
    assert report.ok and report.outcomes["applied"] == 1


def test_verify_reports_silent_loss_when_applied_has_no_effect():
    assert "silent loss" in _scenario("applied", effects=0).violations[0]


def test_verify_reports_a_duplicated_effect():
    assert "applied 2 times" in _scenario("applied", effects=2).violations[0]


def test_verify_reports_a_phantom_effect_for_a_rejected_command():
    assert "phantom" in _scenario("rejected", effects=1, rows=0).violations[0]


def test_verify_reports_a_command_that_never_finished():
    report = _scenario(None, effects=0, rows=0)
    assert report.unfinished and not report.violations and not report.ok


def test_verify_reports_an_accepted_command_missing_from_the_journal():
    ledger = h.Ledger("t")
    issued, _ = ledger.add_task_request("w", "m1")
    issued.accepted = True
    report = h.verify_exactly_once(ledger, FakeApi({}), {"w": FakeNode({}, {})}, settle_seconds=0.0)  # type: ignore[arg-type]
    assert "lost" in report.violations[0]


def test_verify_allows_an_uncertain_command_but_reports_it_and_rejects_a_repeat():
    once = _scenario("uncertain", effects=1)
    assert once.ok and len(once.uncertain) == 1
    assert _scenario("uncertain", effects=0, rows=0).ok
    assert "duplicate" in _scenario("uncertain", effects=2).violations[0]


def test_verify_checks_the_coordinator_audit_when_given_one():
    ledger = h.Ledger("t")
    issued, _ = ledger.add_task_request("w", "m1")
    issued.accepted = True
    node = FakeNode({issued.marker: 1}, {issued.request_id: ["APPLIED"]})
    api = FakeApi({issued.request_id: _done("applied")})
    ok = h.verify_exactly_once(ledger, api, {"w": node}, settle_seconds=0.0, audit=h.Counter({issued.request_id: 1}))  # type: ignore[arg-type]
    twice = h.verify_exactly_once(ledger, api, {"w": node}, settle_seconds=0.0, audit=h.Counter({issued.request_id: 2}))  # type: ignore[arg-type]
    unproven = FakeNode({issued.marker: 1}, {issued.request_id: ["APPLIED", "APPLIED"]})  # a pruned audit row is accepted only with a clean worker proof
    missing = h.verify_exactly_once(ledger, api, {"w": unproven}, settle_seconds=0.0, audit=h.Counter())  # type: ignore[arg-type]
    pruned = h.verify_exactly_once(ledger, api, {"w": node}, settle_seconds=0.0, audit=h.Counter())  # type: ignore[arg-type]
    assert pruned.ok
    assert ok.ok and "2 submit audit entries" in twice.violations[0] and "0 submit audit entries" in missing.violations[0]


def _pruned_scenario(*, effects: int, rows: int, audit: int | None = None, state: str | None = None) -> h.Verification:
    """One accepted command that the coordinator no longer has (404), or still has in ``state``."""
    ledger = h.Ledger("t")
    issued, _ = ledger.add_task_request("w", "m1")
    issued.accepted = True
    node = FakeNode({issued.marker: effects} if effects else {}, {issued.request_id: ["APPLIED"] * rows} if rows else {})
    entries = {issued.request_id: {"state": state, "result": None}} if state else {}
    return h.verify_exactly_once(ledger, FakeApi(entries), {"w": node}, settle_seconds=0.0,  # type: ignore[arg-type]
                                 audit=None if audit is None else h.Counter({issued.request_id: audit} if audit else {}))


def test_verify_accepts_a_command_the_coordinator_pruned_when_the_worker_proves_one_effect():
    # The coordinator deletes old finished commands and audit rows; the worker database is the proof.
    report = _pruned_scenario(effects=1, rows=1, audit=0)
    assert report.ok and report.pruned_by_retention == 1


def test_verify_reports_a_pruned_command_without_an_effect_as_lost():
    report = _pruned_scenario(effects=0, rows=0)
    assert "lost" in report.violations[0] and report.pruned_by_retention == 0


def test_verify_reports_a_pruned_command_with_two_worker_rows_as_a_violation():
    assert "lost" in _pruned_scenario(effects=1, rows=2).violations[0]
    assert "lost" in _pruned_scenario(effects=2, rows=1).violations[0]


def test_verify_reports_an_old_command_that_is_stuck_as_unfinished_not_pruned():
    report = _pruned_scenario(effects=0, rows=0, state="sent")
    assert report.unfinished and not report.violations and report.pruned_by_retention == 0


def test_verify_requires_exactly_one_command_row_on_the_worker():
    assert "command rows" in _scenario("applied", effects=1, rows=2).violations[0]


def test_marker_count_reads_the_real_worker_schema(tmp_path):
    node = h.WorkerNode("unit", tmp_path / "unit")
    try:
        with BridgeStore(node.db_path) as store:
            mission = store.create_mission("m", "objective", cwd=tmp_path)
            store.add_task(mission["id"], "marker:abc")
            store.add_task(mission["id"], "marker:abc-def")
        assert node.marker_count("marker:abc") == 1 and node.marker_count("marker:zzz") == 0
        assert node.command_status("none") == [] and node.mission_statuses() and sum(node.task_statuses().values()) >= 2
        assert node.last_event_id() >= 1 and node.event_ids()
    finally:
        shutil.rmtree(node.runtime, ignore_errors=True)


def test_worker_environment_never_inherits_operator_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", "/operator/state")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("CODEX_HOME", "/operator/codex")
    node = h.WorkerNode("unit", tmp_path / "unit")
    try:
        env = node.env()
    finally:
        shutil.rmtree(node.runtime, ignore_errors=True)
    assert env["LLM_BRIDGE_STATE_DIR"] == str(tmp_path / "unit" / "state") and "TMUX" not in env and "CODEX_HOME" not in env
    assert env["HOME"] == str(tmp_path / "unit" / "home")
    assert env["LLM_SCHEDULER_NO_ACTUAL_SUSPEND"] == "1" and env["LLM_TOOLS_NO_INHIBIT"] == "1"
    assert json.loads(Path(env["LLM_SCHEDULER_USAGE_JSON_FILE"]).read_text())["codex"]["available"] is True


def test_cleanup_stops_every_process_that_carries_the_workers_runtime_directory(tmp_path):
    node = h.WorkerNode("unit", tmp_path / "unit")
    stray = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], env={**node.env()}, start_new_session=True)
    try:
        assert h.scan_processes([node.runtime])[str(node.runtime)] == [stray.pid]
        node.cleanup()
        assert stray.wait(timeout=15) is not None and h.scan_processes([node.runtime]) == {}
    finally:
        stray.kill()
        stray.wait()
        shutil.rmtree(node.runtime, ignore_errors=True)


def test_usage_payload_limit_has_a_future_reset():
    healthy, limited = h.usage_payload(), h.usage_payload(limited_for=30)
    assert healthy["codex"]["five_hour"]["remaining"] > 0
    window = limited["codex"]["five_hour"]
    assert window["remaining"] == 0 and 0 < window["resets_at"] - time.time() <= 31


def test_submit_until_accepted_retries_under_the_same_id_and_stops_on_a_refusal():
    class Api:
        def __init__(self, statuses: list[int]) -> None:
            self.statuses, self.ids = statuses, []

        def timed_submit(self, worker, request, *, wait_ms=0):
            self.ids.append(request["request_id"])
            return self.statuses.pop(0), {}

    ledger = h.Ledger("t")
    issued, request = ledger.add_task_request("w", "m")
    api = Api([0, 0, 202])
    assert h.submit_until_accepted(api, issued, request, deadline=time.monotonic() + 5, pause=0)[0] == 202  # type: ignore[arg-type]
    assert issued.accepted and issued.attempts == 3 and set(api.ids) == {issued.request_id}
    refused, refused_request = ledger.add_task_request("w", "m")
    assert h.submit_until_accepted(Api([429]), refused, refused_request, deadline=time.monotonic() + 5, pause=0)[0] == 429  # type: ignore[arg-type]
    assert not refused.accepted


# ---- fake provider ----------------------------------------------------------------------------------

def _run_provider(tmp_path: Path, force: str, *, timeout: float = 10.0, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "SOAK_PROVIDER_FORCE": force, "SOAK_PROVIDER_SPEED": "100", **env}
    return subprocess.run([sys.executable, str(PROVIDER)], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=timeout)


def test_provider_behaviours_have_distinct_observable_results(tmp_path):
    ok = _run_provider(tmp_path, "output")
    assert ok.returncode == 0 and "done" in ok.stdout
    limited = _run_provider(tmp_path, "ratelimit")
    assert limited.returncode == 1 and "rate limit" in limited.stderr
    crashed = _run_provider(tmp_path, "crash")
    assert crashed.returncode == -signal.SIGKILL
    with pytest.raises(subprocess.TimeoutExpired):
        _run_provider(tmp_path, "question", timeout=2.0)  # waits for an answer; the scheduler aborts it as a blocked prompt
    assert (tmp_path / ".soak-provider-count").read_text() == "4"


def test_provider_choice_is_deterministic_and_follows_the_weights():
    import importlib.util

    weights = [1.0, 1.0, 1.0, 1.0]

    spec = importlib.util.spec_from_file_location("bridge_soak_provider", PROVIDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    draws = [module.choose(5, "/a", n, weights) for n in range(2000)]
    assert draws == [module.choose(5, "/a", n, weights) for n in range(2000)]
    assert set(draws) == set(module.NAMES) and all(350 < draws.count(name) < 650 for name in module.NAMES)
    only_output = [module.choose(5, "/a", n, [1.0, 0.0, 0.0, 0.0]) for n in range(50)]
    assert set(only_output) == {"output"}
    assert module.choose(6, "/b", 1, weights) in module.NAMES
    with pytest.raises(SystemExit):
        module.parse_mix("1,2")


# ---- WebSocket frame proxy --------------------------------------------------------------------------------

class EchoServer:
    """A TLS WebSocket server that answers every text frame ``{"type": T}`` with ``{"type": "echo-T"}``."""

    def __init__(self, directory: Path) -> None:
        self.certificate, key = dist_ws_proxy.make_proxy_certificate(directory)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(self.certificate), str(key))
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.received: list[str] = []
        self.stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        self.listener.settimeout(0.2)
        while not self.stop.is_set():
            try:
                raw, _ = self.listener.accept()
            except OSError:
                continue
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw: socket.socket) -> None:
        try:
            connection = self.context.wrap_socket(raw, server_side=True)
            head = dist_ws_proxy.WsFrameProxy._read_head(connection).decode("latin-1")
            key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n") if line.lower().startswith("sec-websocket-key"))
            connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                f"Sec-WebSocket-Accept: {bridge_ws.accept_key(key)}\r\n\r\n").encode())
            while not self.stop.is_set():
                frame = bridge_ws.read_frame(connection, max_payload=1 << 20, expect_masked=True)
                if frame.opcode == bridge_ws.OP_CLOSE:
                    return
                if frame.opcode == bridge_ws.OP_TEXT:
                    message = json.loads(frame.payload)
                    self.received.append(message["type"])
                    connection.sendall(bridge_ws.encode_frame(bridge_ws.OP_TEXT, json.dumps({"type": "echo-" + message["type"]}).encode(), mask=False))
        except (OSError, bridge_ws.WebSocketError, ssl.SSLError):
            pass

    def close(self) -> None:
        self.stop.set()
        self.listener.close()


@pytest.fixture
def relay(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("the openssl command line tool is required")
    (tmp_path / "echo").mkdir()
    (tmp_path / "proxy").mkdir()
    server = EchoServer(tmp_path / "echo")
    proxy = dist_ws_proxy.WsFrameProxy(server.port, server.certificate, tmp_path / "proxy", seed=3)
    context = ssl.create_default_context(cafile=str(proxy.certificate))

    def connect() -> bridge_ws.WebSocketClient:
        return bridge_ws.WebSocketClient.connect(f"wss://127.0.0.1:{proxy.port}/echo", context=context, timeout=scaled(5))

    try:
        yield server, proxy, connect
    finally:
        proxy.close()
        server.close()


def _ask(client: bridge_ws.WebSocketClient, kind: str, timeout: float = 3.0) -> str | None:
    client.send_text(json.dumps({"type": kind}))
    text = client.receive_text(timeout=timeout)
    return None if text is None else json.loads(text)["type"]


def test_proxy_relays_frames_in_both_directions_with_correct_masking(relay):
    server, proxy, connect = relay
    client = connect()
    assert _ask(client, "a") == "echo-a" and _ask(client, "b") == "echo-b"
    assert server.received == ["a", "b"]
    assert proxy.forwarded[(dist_ws_proxy.UP, "a")] == 1 and proxy.forwarded[(dist_ws_proxy.DOWN, "echo-b")] == 1
    client.close()


def test_proxy_drops_exactly_the_requested_frames(relay):
    server, proxy, connect = relay
    client = connect()
    rule = proxy.drop_next(dist_ws_proxy.UP, "lost")
    assert _ask(client, "lost", timeout=0.5) is None  # swallowed on the way to the server
    assert _ask(client, "kept") == "echo-kept"
    assert server.received == ["kept"] and rule.dropped == 1 and proxy.dropped[(dist_ws_proxy.UP, "lost")] == 1
    proxy.drop_next(dist_ws_proxy.DOWN, "echo-reply")
    assert _ask(client, "reply", timeout=0.5) is None  # the answer is swallowed on the way back
    assert "reply" in server.received and _ask(client, "next") == "echo-next"
    client.close()


def test_proxy_probability_drop_is_seeded_and_clearable(relay):
    server, proxy, connect = relay
    client = connect()
    proxy.set_drop_probability(dist_ws_proxy.UP, 1.0)
    assert _ask(client, "x", timeout=0.4) is None
    proxy.clear_drops()
    assert _ask(client, "y") == "echo-y"
    client.close()


def test_proxy_delay_holds_frames_without_reordering_or_loss(relay):
    server, proxy, connect = relay
    client = connect()
    proxy.set_delay(dist_ws_proxy.UP, 0.3)
    started = time.monotonic()
    assert _ask(client, "slow") == "echo-slow"
    assert time.monotonic() - started >= 0.3
    proxy.clear_delay()
    started = time.monotonic()
    assert _ask(client, "fast") == "echo-fast" and time.monotonic() - started < 0.3
    client.close()


def test_proxy_reset_and_refusal_cut_the_link(relay):
    server, proxy, connect = relay
    client = connect()
    assert _ask(client, "a") == "echo-a"
    assert proxy.reset_all() == 1
    with pytest.raises((bridge_ws.WebSocketError, OSError)):
        _ask(client, "b")
    proxy.refuse()
    with pytest.raises((bridge_ws.WebSocketError, OSError)):
        connect()
    proxy.allow()
    again = connect()
    assert _ask(again, "c") == "echo-c" and proxy.refused >= 1
    again.close()


def test_proxy_reset_ends_every_link_thread_before_it_returns(relay):
    """``reset_all`` must stop the threads that were blocked on the link's sockets, not just close the descriptors.

    A thread still inside a read on a closed descriptor keeps using that descriptor number after the kernel hands it
    to the next connection, and its TLS layer then writes into an unrelated handshake (SSL record layer failure).
    """
    server, proxy, connect = relay
    before = set(threading.enumerate())
    clients = [connect() for _ in range(4)]
    for client in clients:
        assert _ask(client, "a") == "echo-a"
    link_threads = [thread for thread in threading.enumerate() if thread not in before]
    assert len(link_threads) >= 8  # at least a serve thread and a pump thread per link
    assert proxy.reset_all() == 4
    for thread in link_threads:
        thread.join(scaled(2))
    assert [thread.name for thread in link_threads if thread.is_alive()] == []
    again = connect()  # a fresh connection straight after the reset works
    assert _ask(again, "b") == "echo-b"
    again.close()


# ---- link shaping (DOG-019) -------------------------------------------------------------------------------

def test_link_profile_derives_rate_and_delays_from_the_stated_link():
    profile = dist_ws_proxy.LinkProfile()
    assert (profile.rtt_seconds, profile.jitter_seconds, profile.bandwidth_bits) == (0.3, 0.05, 1_000_000.0)
    assert profile.bytes_per_second == 125_000.0 and profile.one_way_seconds == 0.15 and profile.setup_seconds == pytest.approx(0.9)


def test_token_bucket_passes_a_burst_then_paces_at_the_rate():
    bucket = dist_ws_proxy.TokenBucket(rate=1000.0, burst=500.0, clock=lambda: 0.0)
    assert bucket.reserve(500, now=0.0) == 0.0  # the burst goes through at once
    waits = [bucket.reserve(250, now=0.0) for _ in range(4)]
    assert waits == pytest.approx([0.25, 0.5, 0.75, 1.0])  # each further 250 bytes costs 0.25 s of the rate
    assert bucket.bytes_total == 1500


def test_token_bucket_refills_over_idle_time_but_never_beyond_the_burst():
    bucket = dist_ws_proxy.TokenBucket(rate=1000.0, burst=500.0, clock=lambda: 0.0)
    assert bucket.reserve(500, now=0.0) == 0.0
    assert bucket.reserve(250, now=0.25) == 0.0  # a quarter second earned 250 bytes
    assert bucket.reserve(500, now=60.0) == 0.0  # a minute of idle time earns the burst, not 60 000 bytes
    assert bucket.reserve(500, now=60.0) == pytest.approx(0.5)


def test_token_bucket_makes_a_later_caller_wait_for_the_debt_of_an_earlier_one():
    bucket = dist_ws_proxy.TokenBucket(rate=1000.0, burst=100.0, clock=lambda: 0.0)
    assert bucket.reserve(1100, now=0.0) == pytest.approx(1.0)  # 1000 bytes beyond the burst
    assert bucket.reserve(100, now=0.5) == pytest.approx(0.6)  # arrives mid-wait: the debt ends at 1.0, then 100 more bytes
    with pytest.raises(ValueError):
        dist_ws_proxy.TokenBucket(rate=0, burst=1)


def test_token_bucket_reports_peak_and_busy_second_rates():
    bucket = dist_ws_proxy.TokenBucket(rate=1000.0, burst=1000.0, clock=lambda: 0.0)
    for second in range(4):
        bucket.reserve(1000, now=float(second))
    bucket.reserve(100, now=10.0)
    figures = bucket.achieved_bits_per_second()
    assert figures == {"peak_bps": 8000.0, "busy_mean_bps": 8000.0, "busy_seconds": 4.0}


def test_jitter_delay_is_seeded_bounded_and_centred_on_half_the_round_trip():
    first, second = dist_ws_proxy.JitterDelay(0.15, 0.05, seed=9), dist_ws_proxy.JitterDelay(0.15, 0.05, seed=9)
    other = dist_ws_proxy.JitterDelay(0.15, 0.05, seed=10)
    for index in range(5000):
        for delay_line in (first, second, other):
            delay_line.release_time(float(index))  # arrivals a second apart, so the release is arrival plus the drawn delay
    assert first.samples == second.samples and first.samples != other.samples
    assert min(first.samples) >= 0.10 - 1e-9 and max(first.samples) <= 0.20 + 1e-9
    assert sum(first.samples) / len(first.samples) == pytest.approx(0.15, abs=0.003)
    # The jitter is spread over its whole range, not clustered: every tenth of the interval is used.
    buckets = {int((value - 0.10) / 0.01) for value in first.samples}
    assert buckets >= set(range(10))


def test_jitter_delay_never_releases_a_frame_before_the_one_in_front_of_it():
    delay_line = dist_ws_proxy.JitterDelay(0.15, 0.14, seed=4)
    releases = [delay_line.release_time(index * 0.001) for index in range(2000)]  # arrivals much closer than the jitter
    assert releases == sorted(releases)


def test_paced_socket_takes_bytes_at_the_configured_rate():
    left, right = socket.socketpair()
    rate, size = 200_000.0, 100_000
    bucket = dist_ws_proxy.TokenBucket(rate, burst=2048)
    paced = dist_ws_proxy._PacedSocket(right, bucket, threading.Event())
    left.sendall(b"x" * size)
    started = time.monotonic()
    received = 0
    while received < size:
        received += len(paced.recv(65536))
    elapsed = time.monotonic() - started
    left.close()
    right.close()
    expected = (size - 2048) / rate
    assert elapsed >= 0.95 * expected  # never faster than the limit
    assert elapsed <= expected * 1.5 + scaled(0.2)  # and not much slower (sleep granularity, a loaded host)
    assert size / elapsed <= 1.06 * rate + 2048 / elapsed


def test_paced_socket_wakes_when_the_link_is_closed():
    left, right = socket.socketpair()
    closed = threading.Event()
    paced = dist_ws_proxy._PacedSocket(right, dist_ws_proxy.TokenBucket(100.0, burst=10), closed)
    left.sendall(b"y" * 1000)  # 10 s of link time
    threading.Timer(0.2, closed.set).start()
    started = time.monotonic()
    with pytest.raises(OSError):
        paced.recv(1000)
    assert time.monotonic() - started < scaled(3)
    left.close()
    right.close()


@pytest.fixture
def slow_relay(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("the openssl command line tool is required")
    (tmp_path / "echo").mkdir()
    (tmp_path / "proxy").mkdir()
    server = EchoServer(tmp_path / "echo")
    profile = dist_ws_proxy.LinkProfile(rtt_seconds=0.2, jitter_seconds=0.04, bandwidth_bits=1_000_000.0, setup_round_trips=2.0)
    proxy = dist_ws_proxy.WsFrameProxy(server.port, server.certificate, tmp_path / "proxy", seed=5, link=profile)
    context = ssl.create_default_context(cafile=str(proxy.certificate))

    def connect() -> bridge_ws.WebSocketClient:
        return bridge_ws.WebSocketClient.connect(f"wss://127.0.0.1:{proxy.port}/slow/path?x=1", context=context, timeout=scaled(10))

    try:
        yield server, proxy, profile, connect
    finally:
        proxy.close()
        server.close()


def test_slow_link_costs_setup_round_trips_and_delays_each_frame_by_the_profile(slow_relay):
    server, proxy, profile, connect = slow_relay
    started = time.monotonic()
    client = connect()
    assert time.monotonic() - started >= 0.95 * profile.setup_seconds  # two round trips of 0.2 s before the first byte
    assert proxy.accepted_paths == {"/slow/path": 1}
    round_trips = []
    for _ in range(25):
        before = time.monotonic()
        assert _ask(client, "probe", timeout=scaled(5)) == "echo-probe"
        round_trips.append(time.monotonic() - before)
    ordered = sorted(round_trips)
    assert ordered[0] >= profile.rtt_seconds - 2 * profile.jitter_seconds - 0.01  # no round trip is shorter than the profile allows
    assert ordered[len(ordered) // 2] == pytest.approx(profile.rtt_seconds, abs=0.06)  # the median is the stated round trip
    assert ordered[-1] <= profile.rtt_seconds + 2 * profile.jitter_seconds + scaled(0.3)
    stats = proxy.shaper.stats()
    assert stats["one_way_delay"]["up"]["count"] >= 25 and stats["one_way_delay"]["down"]["count"] >= 25
    assert stats["bytes"]["up"] > 0 and stats["bytes"]["down"] > 0
    client.close()


def test_slow_link_limits_bulk_bandwidth_and_a_sender_blocks_while_the_link_drains(slow_relay):
    server, proxy, profile, connect = slow_relay
    client = connect()
    # Pin the sender's send buffer. Left alone, Linux autotunes SO_SNDBUF upward while the transfer runs (46 KB at connect to
    # 184 KB during this send on the development host; the limit is tcp_wmem[2]), so the amount of data the kernel can hold
    # before send() blocks depends on the kernel and its sysctls. Setting SO_SNDBUF switches autotuning off for this socket.
    client.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
    send_buffer = client.sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)  # the kernel doubles the requested size
    receive_buffer = proxy._listener.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)  # accepted sockets inherit it (profile.window_bytes)
    tls_records_in_flight = 2 * 16 * 1024  # one record being encrypted on the client, one being decrypted by the proxy
    # Upper bound on the bytes that can be absorbed without the proxy having read them. The kernel counts per-packet overhead
    # against both buffers, so the payload they really hold is smaller; using the full sizes keeps the bound on the safe side.
    absorbed = send_buffer + receive_buffer + tls_records_in_flight
    padding = "x" * 400_000
    assert absorbed < 300_000, absorbed  # otherwise the frame would fit and nothing would block
    blocked_floor = 0.8 * (400_000 - absorbed) / profile.bytes_per_second
    assert blocked_floor > 0.75, (blocked_floor, absorbed)  # still well above the 0.5 s receive timeout that the worker's sends once inherited
    started = time.monotonic()
    client.send_text(json.dumps({"type": "bulk", "pad": padding}))
    sent_after = time.monotonic() - started
    assert client.receive_text(timeout=scaled(30)) is not None
    total = time.monotonic() - started
    link_seconds = 400_000 / profile.bytes_per_second
    assert total >= 0.9 * link_seconds  # 400 KB cannot arrive faster than 125 KB/s allows
    # The proxy takes bytes at the link rate, so the buffers above fill and the send call is held back while the link drains
    # the rest of the frame.
    assert sent_after >= blocked_floor, (sent_after, blocked_floor, send_buffer, receive_buffer, link_seconds)
    figures = proxy.shaper.stats()["bandwidth"][dist_ws_proxy.UP]
    assert figures["busy_seconds"] >= 1 and figures["busy_mean_bps"] <= 1.1 * profile.bandwidth_bits
    client.close()


def test_a_send_timeout_of_half_a_second_fails_on_the_slow_link_and_the_default_survives(slow_relay):
    """The worker's pre-PR #53 behaviour (sends inherit the 0.5 s receive timeout) cannot carry a large frame on the slow link."""
    server, proxy, profile, connect = slow_relay
    message = json.dumps({"type": "bulk", "pad": "x" * 450_000})
    short = connect()
    short.send_timeout = 0.5
    with pytest.raises(OSError):  # TimeoutError: the write blocked for more than 0.5 s
        short.send_text(message)
    healthy = connect()
    healthy.send_text(message)  # the default send timeout is long enough
    assert healthy.receive_text(timeout=scaled(30)) is not None
    healthy.close()


def test_frame_message_type_reads_only_protocol_messages():
    assert dist_ws_proxy.frame_message_type(b'{"type":"command","v":1}') == "command"
    assert dist_ws_proxy.frame_message_type(b"not json") is None and dist_ws_proxy.frame_message_type(b'{"type":7}') is None
    assert dist_ws_proxy.frame_message_type(b"[1]") is None


# ---- end to end -------------------------------------------------------------------------------------------

@requires_coordinator
def test_short_soak_with_real_coordinator_workers_and_fake_providers(tmp_path):
    """About a minute: two workers, four missions, commands with unique IDs, one injected capacity limit; every criterion must pass."""
    report_path = tmp_path / "report.json"
    started = time.monotonic()
    # This run checks that the harness works. Its debug-build coordinator shares two CI vCPUs with six
    # worker and controller processes and the load generator, and a 60 s run has about 250 samples per
    # class, so its p99 is the third slowest request. Measured here (three runs, load average 15 to 45):
    # read p99 70 to 137 ms and submit p99 95 to 191 ms, against the 250 ms and 500 ms budgets; CI
    # reported 371 ms and 500.3 ms. The budgets of PRD-007 are enforced by the long soak (soak-nightly.yml),
    # so this test uses four times their value and still fails on a seconds-long stall.
    code = bridge_soak.main([
        "--duration", str(int(scaled(60))), "--workers", "2", "--missions", "4", "--sample-seconds", "5", "--warmup-seconds", "30", "--limit-every", "20", "--limit-seconds", "8",
        "--settle-seconds", str(int(scaled(60))), "--count-floor-multiplier", "8", "--command-rate", "3", "--backlog", "10", "--run-dir", str(tmp_path / "run"), "--report", str(report_path),
        "--p99-read-ms", str(4 * h.DEFAULT_LATENCY_BUDGET_MS["read"]), "--p99-submit-ms", str(4 * h.DEFAULT_LATENCY_BUDGET_MS["submit"]),
        # A 30 s fit cannot separate the coordinator's allocator ramp from a leak: its RSS rose 2.7 to 7.2 MiB over the window in 10 loaded runs
        # (2 of 10 failed the 4 MiB floor). Judge the absolute rise against 12 MiB instead; the full-size soak keeps the slope bound and the 4 MiB floor.
        "--coordinator-rss-floor-kib", "12288",
    ])
    report = json.loads(report_path.read_text())
    failed = [item for item in report["checks"] if item["verdict"] != "pass"]
    assert code == 0 and report["passed"] and not failed, failed
    assert report["commands"]["accepted"] >= 50 and report["commands"]["violation_count"] == 0
    assert len(report["samples"]) >= 6 and {item["class"] for item in report["latency"]} == {"read", "submit", "roundtrip"}
    assert report["workload"]["task_statuses"].get("COMPLETED", 0) > 0
    assert time.monotonic() - started < scaled(300)


def test_short_soak_judges_the_coordinator_rss_rise_absolutely_and_still_fails_a_leak():
    """The coordinator's RSS is still ramping 30 to 60 s after the start. The worst of 10 loaded runs rose 7.2 MiB over the fit window, which the
    default 4 MiB floor failed (slope 873740/h). A leak of 500 KiB per second must still fail with the 12 MiB floor the short soak uses."""
    ramp = [(30.0, 32304.0), (35.0, 32532.0), (40.0, 32748.0), (46.0, 34656.0), (51.0, 35408.0), (56.0, 37020.0), (60.0, 39796.0)]
    default = h.DEFAULT_BOUNDS["coordinator_rss_kib"]
    short = h.SlopeBound(default.per_hour, 12288.0, default.unit)
    assert h.check_slope("coordinator_rss_kib", ramp, default, warmup_seconds=0.0).verdict == "fail"
    assert h.check_slope("coordinator_rss_kib", ramp, short, warmup_seconds=0.0).verdict == "pass"
    leaking = [(t, kib + 500.0 * (t - 30.0)) for t, kib in ramp]
    assert h.check_slope("coordinator_rss_kib", leaking, short, warmup_seconds=0.0).verdict == "fail"
    assert bridge_soak.chosen_bounds(bridge_soak.parse_args(["--coordinator-rss-floor-kib", "12288"]))["coordinator_rss_kib"].floor == 12288.0
    assert bridge_soak.chosen_bounds(bridge_soak.parse_args([]))["coordinator_rss_kib"].floor == default.floor  # the full-size soak is unchanged


def test_coordinator_memory_growth_with_its_rows_is_not_a_leak_but_growth_without_rows_is():
    bound = {"coordinator_rss_kib": h.SlopeBound(per_hour=1024.0, floor=512.0, unit="KiB")}

    def run(rows_per_sample: int):
        samples = [{"t": index * 30.0, "coordinator": {"rss_kib": 20_000 + index * 100}, "rows": {"events": index * rows_per_sample}} for index in range(60)]
        return h.evaluate_resources(samples, bound, warmup_seconds=300.0, ceilings={})[0]

    assert run(rows_per_sample=1000).verdict == "pass"  # 100 KiB per 1000 rows is 102 bytes per row, under the allowance
    assert run(rows_per_sample=0).verdict == "fail"  # the same growth with no new rows is a leak


def _fleet(slope_kib_per_hour: float, *, processes: int = 5, restart_at: float | None = None) -> list[dict]:
    samples = []
    for index in range(80):
        t = index * 30.0
        by_pid = {}
        for number in range(processes):
            started = restart_at if (restart_at is not None and number == 0 and t >= restart_at) else 0.0
            by_pid[str(100 + number + (1000 if started else 0))] = 36_000 + 4_000 * (1 - 0.5 ** ((t - started) / 300.0)) + slope_kib_per_hour / 3600.0 * max(0.0, t - started - 900.0)
        samples.append({"t": t, "controllers": {"rss_kib": sum(by_pid.values()), "running": len(by_pid), "by_pid": by_pid}})
    return samples


def test_controllers_are_judged_per_process_after_each_ones_own_warmup():
    bound = {"controllers_rss_kib": h.SlopeBound(per_hour=512.0, floor=256.0, unit="KiB", per_process=True)}
    scaled = h.scale_bounds(bound, workers=1, controllers=5)

    def verdict(rate, **kw):
        return h.evaluate_resources(_fleet(rate, **kw), scaled, warmup_seconds=1200.0, ceilings={}, process_bounds=bound)[0]

    assert verdict(0.0).verdict == "pass"
    assert verdict(4000.0).verdict == "fail" and "median slope" in verdict(4000.0).detail


def test_a_restarted_controller_does_not_look_like_a_leak_in_the_fleet():
    bound = {"controllers_rss_kib": h.SlopeBound(per_hour=512.0, floor=256.0, unit="KiB", per_process=True)}
    scaled = h.scale_bounds(bound, workers=1, controllers=5)
    samples = _fleet(0.0, restart_at=900.0)  # one controller is replaced half way: its pid changes and its memory starts over
    summed = h.check_slope("controllers_rss_kib", [(s["t"], s["controllers"]["rss_kib"]) for s in samples], scaled["controllers_rss_kib"], warmup_seconds=1200.0)
    per_process = h.evaluate_resources(samples, scaled, warmup_seconds=1200.0, ceilings={}, process_bounds=bound)[0]
    assert per_process.verdict == "pass", per_process.detail
    assert "median" in per_process.detail and summed.metric == "controllers_rss_kib"


def test_per_process_check_is_inconclusive_when_no_process_lives_past_its_warmup():
    bound = h.SlopeBound(per_hour=512.0, floor=256.0, unit="KiB", per_process=True)
    result = h.check_per_process_slope("controllers_rss_kib", {"1": [(0.0, 1.0), (30.0, 1.0)]}, bound, warmup_seconds=1200.0)
    assert result.verdict == "inconclusive"


def _worker_fleet(slope_kib_per_hour: float, *, processes: int = 4, samples_count: int = 240) -> list[dict]:
    """Workers rise by 1.8 MiB while they settle (measured), then move by ``slope_kib_per_hour``."""
    samples = []
    for index in range(samples_count):
        t = index * 30.0
        by_pid = {str(200 + number): 32_000 + 1_800 * (1 - 0.5 ** (t / 400.0)) + (number * 700) + slope_kib_per_hour / 3600.0 * max(0.0, t - 1200.0)
                  for number in range(processes)}
        samples.append({"t": t, "workers": {"rss_kib": sum(by_pid.values()), "running": len(by_pid), "by_pid": by_pid}})
    return samples


def test_workers_are_judged_per_process_with_a_bound_below_the_old_three_mib_per_hour():
    bound = {"workers_rss_kib": h.DEFAULT_BOUNDS["workers_rss_kib"]}
    assert bound["workers_rss_kib"].per_hour <= 512 and bound["workers_rss_kib"].per_process
    scaled = h.scale_bounds(bound, workers=4, controllers=1)

    def verdict(rate: float):
        return h.evaluate_resources(_worker_fleet(rate), scaled, warmup_seconds=1200.0, ceilings={}, process_bounds=bound)[0]

    assert verdict(0.0).verdict == "pass"
    assert verdict(200.0).verdict == "pass"  # the largest slope measured after warm-up in a 3 hour run is 225 KiB per hour
    leaking = verdict(2048.0)  # the rate first read from the 45-minute soak; the old bound of 3 MiB per hour let it pass
    assert leaking.verdict == "fail" and "median slope" in leaking.detail
    summed = h.check_slope("workers_rss_kib", [(s["t"], s["workers"]["rss_kib"]) for s in _worker_fleet(2048.0)],
                           h.SlopeBound(3 * 1024 * 4, 1024 * 4, "KiB"), warmup_seconds=1200.0)
    assert summed.verdict == "pass"


# Median resident size (MiB) of the 50 controllers, one sample every 60 s from t=60 s, recorded in the 4.5-hour nightly run 43134569798
# (GitHub-hosted runner). The jumps at 900 s and 1800 s are the 900 s retention pass of the capacity refresh worker; the
# series is still settling until about 1800 s. The first 30 values are the same as in the 30-minute nightly run of that day.
RECORDED_CONTROLLER_MEDIAN_MIB = [
    32.068, 32.225, 32.377, 32.508, 32.6, 32.705, 32.828, 32.936, 33.051, 33.176, 33.271, 33.387, 33.496, 33.613, 35.551,
    34.822, 34.896, 35.006, 35.078, 35.178, 35.551, 35.754, 35.814, 35.863, 35.887, 35.9, 35.922, 35.928, 35.943, 36.867,
    36.035, 36.049, 36.07, 36.074, 36.088, 36.094, 36.119, 36.137, 36.145, 36.156, 36.164, 36.184, 36.188, 36.191, 36.441]


def _recorded_controllers(samples: int, leak_kib_per_hour: float = 0.0, leak_from: float = 1800.0) -> dict[str, list[tuple[float, float]]]:
    series = [((index + 1) * 60.0, mib * 1024.0) for index, mib in enumerate(RECORDED_CONTROLLER_MEDIAN_MIB[:samples])]
    return {str(pid): [(t, kib + pid * 8.0 + leak_kib_per_hour / 3600.0 * max(0.0, t - leak_from)) for t, kib in series] for pid in range(5)}


def test_recorded_nightly_series_fails_the_old_30_minute_gate_and_passes_the_45_minute_gate():
    bound = h.SlopeBound(1536, 512, "KiB", per_process=True)
    old = h.check_per_process_slope("controllers_rss_kib", _recorded_controllers(30), bound, warmup_seconds=1200.0)
    assert old.verdict == "fail" and old.slope_per_hour > 4000  # the 1800 s retention pass lands on the last sample of a 10-minute fit
    new = h.check_per_process_slope("controllers_rss_kib", _recorded_controllers(45), bound, warmup_seconds=bridge_soak.WARMUP_SECONDS)
    assert bridge_soak.WARMUP_SECONDS == 1800.0 and new.verdict == "pass" and abs(new.slope_per_hour) < 1536


def test_the_45_minute_gate_still_fails_a_leak_above_what_its_floor_can_resolve():
    bound = h.SlopeBound(1536, 512, "KiB", per_process=True)
    # A 900 s fit can only see a rise above the 512 KiB floor: 2.5 MiB per hour is 640 KiB in the window. Smaller leaks are for the long run.
    leaking = _recorded_controllers(45, leak_kib_per_hour=2.5 * 1024)
    assert h.check_per_process_slope("controllers_rss_kib", leaking, bound, warmup_seconds=1800.0).verdict == "fail"


def test_a_growing_worker_command_backlog_is_reported():
    ok = [{"t": 1.0, "queue_depths": {"w1": {"pending": 3, "sent": 1}}}, {"t": 2.0, "queue_depths": {}}, {"t": 3.0, "queue_depths": None}]
    bad = [{"t": 61.0, "queue_depths": {"w1": {"pending": 90, "sent": 20}, "w2": {"pending": 1}}}]
    assert h.check_command_backlog(ok) == []
    assert h.check_command_backlog([{"t": 1.0}, {"t": 2.0, "queue_depths": None}]) is None  # no sample carries the field: inconclusive
    assert h.check_command_backlog(bad) == ["t=61s: w1 has 110 unfinished commands (limit 100)"]
