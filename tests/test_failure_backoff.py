"""ORCH-012: bounded exponential backoff before the same provider is launched again after a failure."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from llm_tools import common, ralph_robin
from llm_tools.common import FailureBackoff, FailureBackoffPolicy, failure_backoff_policy


def test_policy_doubles_from_base_up_to_cap() -> None:
    policy = FailureBackoffPolicy(base=30, cap=900)
    assert [policy.delay(n) for n in range(1, 8)] == [30, 60, 120, 240, 480, 900, 900]
    assert policy.delay(0) == 0
    assert policy.delay(10_000) == 900  # no overflow for a long streak
    assert FailureBackoffPolicy(base=0).delay(3) == 0  # base 0 disables the delay


def test_policy_reads_env_then_config_then_defaults() -> None:
    assert failure_backoff_policy({}, {}) == FailureBackoffPolicy(30, 900)
    assert failure_backoff_policy({"failure_backoff_base": 5, "failure_backoff_max": 50}, {}) == FailureBackoffPolicy(5, 50)
    env = {"LLM_TOOLS_FAILURE_BACKOFF_BASE": "7", "LLM_TOOLS_FAILURE_BACKOFF_MAX": "bad"}
    assert failure_backoff_policy({"failure_backoff_base": 5, "failure_backoff_max": 50}, env) == FailureBackoffPolicy(7, 50)


def test_tracker_counts_per_key_and_resets_on_success() -> None:
    tracker = FailureBackoff(FailureBackoffPolicy(30, 900))
    assert tracker.record_failure("claude", 1000) == 30
    assert tracker.record_failure("claude", 1030) == 60
    assert tracker.blocked(1031) == {"claude"}
    assert tracker.blocked(1090) == set()
    assert tracker.record_failure("codex", 1031) == 30  # another provider has its own count
    tracker.record_success("claude")
    assert tracker.record_failure("claude", 2000) == 30
    assert tracker.earliest_release({"claude", "codex"}, 1040) == 1061
    assert tracker.earliest_release({"claude"}, 1999) == 2030


class _Sim:
    """Fake clock plus a scripted provider; no real sleeping."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, statuses: Callable[[str, int], int]) -> None:
        self.clock = 0
        self.launches: list[tuple[str, int]] = []
        self.statuses = statuses
        self.rate_limit_until = 0
        self.launched = False
        self.tmp_path = tmp_path
        monkeypatch.setattr(common, "migrate_legacy_cache_dirs", lambda: None)
        monkeypatch.setattr(common, "now_epoch", lambda env=None: self.clock)
        monkeypatch.setattr(ralph_robin, "monotonic", lambda: float(self.clock))
        monkeypatch.setattr(ralph_robin, "sleep_seconds", self.sleep)
        monkeypatch.setattr(ralph_robin, "select_provider", self.select)
        monkeypatch.setattr(ralph_robin, "run_scheduler_inline", self.run)
        monkeypatch.setenv("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "1")
        monkeypatch.setenv("LLM_RALPH_WAIT_POLL_SECONDS", "100000")
        monkeypatch.setenv("LLM_TOOLS_FAILURE_BACKOFF_BASE", "30")
        monkeypatch.setenv("LLM_TOOLS_FAILURE_BACKOFF_MAX", "900")

    def sleep(self, seconds: float) -> None:
        self.clock += int(seconds)

    def select(self, cfg: Any, logs: Any, current_index: int, skipped: set[str]) -> dict[str, Any]:
        providers = list(cfg.providers)
        if self.launched and self.clock < self.rate_limit_until and not skipped:
            return {
                "index": 0, "provider": providers[0], "rotation_reason": "all-unusable", "all_rate_limited": True,
                "decision": {"provider": providers[0], "wait_until": self.rate_limit_until},
                "decisions": [{"provider": providers[0], "wait_until": self.rate_limit_until}],
            }
        for index, provider in enumerate(providers):
            if provider not in skipped:
                return {
                    "index": index, "provider": provider, "rotation_reason": "current", "all_rate_limited": False,
                    "decision": {"provider": provider, "usable": True, "wait_until": None},
                    "decisions": [{"provider": provider, "usable": True, "wait_until": None}],
                }
        return {"index": -1, "provider": "", "rotation_reason": "all-skipped", "all_rate_limited": False,
                "decision": {"usable": False, "reason": "all-skipped"}, "decisions": []}

    def run(self, scfg: Any) -> int:
        self.launched = True
        self.launches.append((scfg.provider, self.clock))
        return self.statuses(scfg.provider, len(self.launches))

    def times(self) -> list[int]:
        return [t for _provider, t in self.launches]

    def main(self, providers: str, *extra: str) -> int:
        return ralph_robin.main([
            "--prompt", "x", "--providers", providers,
            "--log-dir", str(self.tmp_path / "logs"), "--state-file", str(self.tmp_path / "state.json"),
            "--min-iteration-seconds", "0", *extra,
        ])


def test_always_failing_provider_is_launched_at_capped_exponential_intervals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ralph_robin, "HARD_FAIL_ABORT_STREAK", 1000)  # leave only the time budget
    sim = _Sim(monkeypatch, tmp_path, lambda provider, n: 1)
    rc = sim.main("claude", "--max-iterations", "0", "--max-duration", "1h")
    assert rc == common.AUTONOMY_ABORT_STATUS  # the budget ended with no completed increment
    # Delays 30, 60, 120, 240, 480, then the 900 s cap; the next launch would fall after 3600 s.
    assert sim.times() == [0, 30, 90, 210, 450, 930, 1830, 2730]


def test_count_budget_still_stops_a_failing_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = _Sim(monkeypatch, tmp_path, lambda provider, n: 1)
    assert sim.main("claude", "--max-iterations", "0", "--max-duration", "0") == 1
    assert len(sim.launches) == ralph_robin.HARD_FAIL_ABORT_STREAK
    assert sim.times() == [0, 30, 90, 210, 450, 930]


def test_autonomy_abort_is_backed_off_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = _Sim(monkeypatch, tmp_path, lambda provider, n: common.AUTONOMY_ABORT_STATUS if n < 4 else 0)
    assert sim.main("claude", "--max-iterations", "1", "--max-duration", "0") == 0
    assert sim.times() == [0, 30, 90, 210]


def test_success_resets_the_delay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = {1: 1, 2: 1, 3: 0, 4: 1, 5: 0}
    sim = _Sim(monkeypatch, tmp_path, lambda provider, n: script[n])
    assert sim.main("claude", "--max-iterations", "2", "--max-duration", "0") == 0
    # fail (wait 30), fail (wait 60), success; the next failure waits 30 again, not 120.
    assert sim.times() == [0, 30, 90, 90, 120]


def test_rate_limit_uses_the_reset_wait_not_the_backoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = _Sim(monkeypatch, tmp_path, lambda provider, n: 1 if n == 1 else 0)
    sim.rate_limit_until = 5000  # capacity read after the failed run shows a limit until epoch 5000
    assert sim.main("claude", "--max-iterations", "1", "--max-duration", "0") == 0
    assert sim.times() == [0, 5000]  # not 30


def test_another_ready_provider_is_not_delayed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sim = _Sim(monkeypatch, tmp_path, lambda provider, n: 1 if provider == "claude" else 0)
    assert sim.main("claude,codex", "--max-iterations", "1", "--max-duration", "0") == 0
    assert sim.launches == [("claude", 0), ("codex", 0)]


def test_base_zero_disables_the_delay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ralph_robin, "HARD_FAIL_ABORT_STREAK", 3)
    sim = _Sim(monkeypatch, tmp_path, lambda provider, n: 1)
    monkeypatch.setenv("LLM_TOOLS_FAILURE_BACKOFF_BASE", "0")
    sim.main("claude", "--max-iterations", "0", "--max-duration", "0")
    assert len(sim.launches) == 3
    assert sim.times() == [0, 60, 120]  # the unchanged flat poll-interval wait, no exponential delay
