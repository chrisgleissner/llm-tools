"""Deterministic unit tests for the suspend/wake reliability core and the
llm-sleep-soak tool.

The real suspend path actually sleeps the host, so it cannot run in CI. Here the
suspend primitive (``subprocess``/``now_epoch``/``suspend_with_wake``) is stubbed
so the *policy* -- wake arming, drift-based verification, the durable cycle
ledger, the watchdog, the idle inhibitor, churn caps, and the soak accounting --
is exercised hermetically.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from llm_tools import common, sleep_soak


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def _cache_env(tmp_path: Path) -> dict[str, str]:
    return {"XDG_CACHE_HOME": str(tmp_path / "cache"), "HOME": str(tmp_path / "home")}


# --------------------------------------------------------------------------- #
# RTC wakealarm + backend detection
# --------------------------------------------------------------------------- #


def test_power_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    assert common.power_backend() == "systemd"
    monkeypatch.setattr(common, "have_cmd", lambda name: name == "systemctl")
    assert common.power_backend() == "none"


def test_read_rtc_wakealarm(tmp_path: Path) -> None:
    alarm = tmp_path / "wakealarm"
    env = {"LLM_TOOLS_RTC_WAKEALARM": str(alarm)}
    assert common.read_rtc_wakealarm(env) is None  # missing file
    alarm.write_text("\n")
    assert common.read_rtc_wakealarm(env) is None  # empty == disarmed
    alarm.write_text("0\n")
    assert common.read_rtc_wakealarm(env) is None  # 0 == disarmed
    alarm.write_text("1781498400\n")
    assert common.read_rtc_wakealarm(env) == 1781498400
    alarm.write_text("not-a-number")
    assert common.read_rtc_wakealarm(env) is None


def test_suspend_drift_tolerance() -> None:
    assert common.suspend_drift_tolerance({}) == 90
    assert common.suspend_drift_tolerance({"LLM_TOOLS_SUSPEND_DRIFT_TOLERANCE": "30"}) == 30
    assert common.suspend_drift_tolerance({"LLM_TOOLS_SUSPEND_DRIFT_TOLERANCE": "1"}) == 5  # floored
    assert common.suspend_drift_tolerance({"LLM_TOOLS_SUSPEND_DRIFT_TOLERANCE": "x"}) == 90


# --------------------------------------------------------------------------- #
# Idle inhibitor
# --------------------------------------------------------------------------- #


def test_idle_inhibitor_disabled_by_env() -> None:
    inh = common.IdleSuspendInhibitor("t", "why", {"LLM_TOOLS_NO_INHIBIT": "1"})
    assert inh.acquire() is False
    inh.release()  # no-op, must not raise


def test_idle_inhibitor_acquire_release(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: object) -> None:
            started.append(args)
            self._stdin = kwargs.get("stdin")

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:  # pragma: no cover - defensive
            pass

    monkeypatch.setattr(common, "have_cmd", lambda name: name == "systemd-inhibit")
    monkeypatch.setattr(common.subprocess, "Popen", FakePopen)
    inh = common.IdleSuspendInhibitor("ralph-robin", "busy", {"LLM_TOOLS_NO_INHIBIT": "0"})
    assert inh.acquire() is True
    assert started and started[0][0] == "systemd-inhibit"
    assert "--what=idle" in started[0] and "--mode=block" in started[0]
    inh.release()


def test_idle_inhibitor_missing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    assert common.IdleSuspendInhibitor("t", "w", {"LLM_TOOLS_NO_INHIBIT": "0"}).acquire() is False


# --------------------------------------------------------------------------- #
# Watchdog
# --------------------------------------------------------------------------- #


def test_watchdog_unavailable(tmp_path: Path) -> None:
    env = {"LLM_TOOLS_WATCHDOG_DEVICE": str(tmp_path / "nope")}
    assert common.watchdog_available(env) is False
    dog = common.Watchdog(env)
    assert dog.arm(60) is False
    dog.keepalive()  # no-op
    dog.disarm()  # no-op


def test_watchdog_arm_disarm_fake_device(tmp_path: Path) -> None:
    device = tmp_path / "watchdog"
    device.write_bytes(b"")
    env = {"LLM_TOOLS_WATCHDOG_DEVICE": str(device)}
    assert common.watchdog_available(env) is True
    dog = common.Watchdog(env)
    # ioctl on a regular file fails but the open succeeds -> still "armed".
    assert dog.arm(120) is True
    dog.keepalive()
    dog.disarm()
    # disarm writes the magic 'V' before close on a real char device.
    assert device.read_bytes().endswith(b"V")


def test_watchdog_disabled_by_env(tmp_path: Path) -> None:
    device = tmp_path / "watchdog"
    device.write_bytes(b"")
    env = {"LLM_TOOLS_WATCHDOG_DEVICE": str(device), "LLM_TOOLS_NO_WATCHDOG": "1"}
    assert common.Watchdog(env).arm(60) is False


# --------------------------------------------------------------------------- #
# Suspend ledger
# --------------------------------------------------------------------------- #


def test_suspend_ledger_roundtrip(tmp_path: Path) -> None:
    env = _cache_env(tmp_path)
    path = common.suspend_ledger_path(env)
    common.ledger_record_start(path, "c1", 2000, env, who="ralph-robin")
    common.ledger_record_start(path, "c2", 3000, env)
    common.ledger_record_done(path, "c1", 2001, True, env, drift_seconds=1)
    # c2 has a start but no done -> a wedged/in-flight cycle.
    incomplete = common.incomplete_suspend_cycles(path)
    assert [c["cycle_id"] for c in incomplete] == ["c2"]


def test_incomplete_suspend_cycles_missing_file(tmp_path: Path) -> None:
    assert common.incomplete_suspend_cycles(tmp_path / "absent.jsonl") == []


def test_trim_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text("\n".join(json.dumps({"event": "x", "i": i}) for i in range(500)) + "\n")
    common.trim_ledger(path, keep=100)
    assert len(path.read_text().splitlines()) == 100


# --------------------------------------------------------------------------- #
# arm_rtc_wake
# --------------------------------------------------------------------------- #


def test_arm_rtc_wake_rtcwake_confirmed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    alarm = tmp_path / "wakealarm"
    alarm.write_text("2000")
    env = {"LLM_TOOLS_RTC_WAKEALARM": str(alarm), "LLM_USAGE_NOW_EPOCH": "1000"}
    monkeypatch.setattr(common, "have_cmd", lambda name: name == "rtcwake")
    monkeypatch.setattr(common.subprocess, "run", lambda *a, **k: FakeProc(0, "rtcwake ok"))
    arm = common.arm_rtc_wake(2000, "soak", env)
    assert arm.armed and arm.method == "rtcwake" and arm.confirmed is True


def test_arm_rtc_wake_rejects_wrong_rtcwake_readback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    alarm = tmp_path / "wakealarm"
    alarm.write_text("9999")
    env = {"LLM_TOOLS_RTC_WAKEALARM": str(alarm), "LLM_USAGE_NOW_EPOCH": "1000"}
    monkeypatch.setattr(common, "have_cmd", lambda name: name == "rtcwake")
    monkeypatch.setattr(common.subprocess, "run", lambda *a, **k: FakeProc(0, "rtcwake ok"))
    arm = common.arm_rtc_wake(2000, "soak", env)
    assert arm.armed is False and arm.method == "none"


def test_arm_rtc_wake_falls_back_after_wrong_rtcwake_readback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    alarm = tmp_path / "wakealarm"
    alarm.write_text("9999")
    env = {"LLM_TOOLS_RTC_WAKEALARM": str(alarm), "LLM_USAGE_NOW_EPOCH": "1000"}
    calls: list[list[str]] = []
    monkeypatch.setattr(common, "have_cmd", lambda name: name in ("rtcwake", "systemctl", "systemd-run"))

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[0] == "rtcwake":
            return FakeProc(0, "rtcwake ok")
        if args[0] == "systemd-run":
            return FakeProc(0, "Running timer as unit: x.timer")
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return FakeProc(0, "")
        return FakeProc(0, "")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    arm = common.arm_rtc_wake(2000, "soak", env)
    assert arm.armed and arm.method == "systemd-timer"
    assert any(call[0] == "rtcwake" for call in calls)
    assert any(call[0] == "systemd-run" for call in calls)


def test_arm_rtc_wake_systemd_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"LLM_USAGE_NOW_EPOCH": "1000"}
    monkeypatch.setattr(common, "have_cmd", lambda name: name in ("systemctl", "systemd-run"))

    def fake_run(args, **kwargs):
        if args[0] == "systemd-run":
            return FakeProc(0, "Running timer as unit: x.timer")
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return FakeProc(0, "")
        return FakeProc(0, "")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    arm = common.arm_rtc_wake(5000, "ralph-robin", env)
    assert arm.armed and arm.method == "systemd-timer" and arm.confirmed is False and arm.unit.startswith("ralph-robin-wake-")


def test_arm_rtc_wake_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    arm = common.arm_rtc_wake(5000, "soak", {"LLM_USAGE_NOW_EPOCH": "1000"})
    assert arm.armed is False and arm.method == "none"


def test_arm_rtc_wake_systemd_timer_not_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: name in ("systemctl", "systemd-run"))

    def fake_run(args, **kwargs):
        if args[0] == "systemd-run":
            return FakeProc(0, "")
        return FakeProc(1, "")  # is-active fails

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    arm = common.arm_rtc_wake(5000, "ralph-robin", {"LLM_USAGE_NOW_EPOCH": "1000"})
    assert arm.armed is False


# --------------------------------------------------------------------------- #
# suspend_with_wake orchestration
# --------------------------------------------------------------------------- #


def test_suspend_with_wake_simulated() -> None:
    out = common.suspend_with_wake(2000, who="t", env={"LLM_SCHEDULER_NO_ACTUAL_SUSPEND": "1", "LLM_USAGE_NOW_EPOCH": "1000"})
    assert out.suspended is False and out.reason == "simulated" and out.reliable is True


def test_suspend_with_wake_missing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    out = common.suspend_with_wake(5000, who="t", env={"LLM_USAGE_NOW_EPOCH": "1000"})
    assert out.suspended is False and out.reason == "missing-backend"


def test_suspend_with_wake_insufficient_lead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    out = common.suspend_with_wake(1010, who="t", env={"LLM_USAGE_NOW_EPOCH": "1000", "LLM_SCHEDULER_SUSPEND_MIN_LEAD": "120"})
    assert out.suspended is False and out.reason == "insufficient-lead"


def test_suspend_with_wake_arm_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = _cache_env(tmp_path) | {"LLM_USAGE_NOW_EPOCH": "1000", "LLM_SCHEDULER_SUSPEND_MIN_LEAD": "10"}
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    monkeypatch.setattr(common, "arm_rtc_wake", lambda *a, **k: common.WakeArm(False, "none", False))
    out = common.suspend_with_wake(5000, who="t", env=env)
    assert out.suspended is False and out.reason == "arm-failed"


def test_suspend_with_wake_success_reliable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = _cache_env(tmp_path) | {"LLM_SCHEDULER_SUSPEND_MIN_LEAD": "10"}
    clock = {"t": 1000}
    monkeypatch.setattr(common, "now_epoch", lambda e=None: clock["t"])
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    monkeypatch.setattr(common, "arm_rtc_wake", lambda *a, **k: common.WakeArm(True, "systemd-timer", False, unit="u"))

    def fake_run(args, **kwargs):
        return FakeProc(0, "")

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    # Simulate the machine sleeping to the target: the wall-clock wait lands at 2000.
    def fake_wait(target, e=None):
        clock["t"] = target
    monkeypatch.setattr(common, "wall_clock_wait_until", fake_wait)

    out = common.suspend_with_wake(2000, who="ralph-robin", env=env)
    assert out.suspended is True and out.reliable is True and out.drift_seconds == 0
    # Ledger has a matching start+done -> nothing incomplete.
    assert common.incomplete_suspend_cycles(common.suspend_ledger_path(env)) == []


def test_suspend_with_wake_unreliable_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = _cache_env(tmp_path) | {"LLM_SCHEDULER_SUSPEND_MIN_LEAD": "10", "LLM_TOOLS_SUSPEND_DRIFT_TOLERANCE": "60"}
    clock = {"t": 1000}
    monkeypatch.setattr(common, "now_epoch", lambda e=None: clock["t"])
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    monkeypatch.setattr(common, "arm_rtc_wake", lambda *a, **k: common.WakeArm(True, "systemd-timer", False, unit="u"))
    monkeypatch.setattr(common.subprocess, "run", lambda *a, **k: FakeProc(0, ""))
    # Woke 1000s after target -> RTC clearly did not fire on time.
    monkeypatch.setattr(common, "wall_clock_wait_until", lambda target, e=None: clock.update(t=target + 1000))
    out = common.suspend_with_wake(2000, who="ralph-robin", env=env)
    assert out.suspended is True and out.reliable is False and out.drift_seconds == 1000


def test_suspend_with_wake_arms_watchdog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = _cache_env(tmp_path) | {"LLM_SCHEDULER_SUSPEND_MIN_LEAD": "10"}
    clock = {"t": 1000}
    armed = {"timeout": None, "disarmed": False}

    class FakeDog:
        def __init__(self, e=None):
            pass

        def arm(self, timeout):
            armed["timeout"] = timeout
            return True

        def keepalive(self):
            pass

        def disarm(self):
            armed["disarmed"] = True

    monkeypatch.setattr(common, "now_epoch", lambda e=None: clock["t"])
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    monkeypatch.setattr(common, "arm_rtc_wake", lambda *a, **k: common.WakeArm(True, "systemd-timer", False, unit="u"))
    monkeypatch.setattr(common.subprocess, "run", lambda *a, **k: FakeProc(0, ""))
    monkeypatch.setattr(common, "wall_clock_wait_until", lambda target, e=None: clock.update(t=target))
    monkeypatch.setattr(common, "Watchdog", FakeDog)
    out = common.suspend_with_wake(2000, who="ralph-robin", watchdog=True, env=env)
    assert out.suspended is True and armed["timeout"] is not None and armed["disarmed"] is True


def test_wall_clock_wait_until(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1000}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += int(seconds)

    monkeypatch.setattr(common, "now_epoch", lambda e=None: clock["t"])
    monkeypatch.setattr(common.time, "sleep", fake_sleep)
    common.wall_clock_wait_until(1030, {"LLM_TOOLS_WAIT_POLL_SECONDS": "10"})
    assert sum(sleeps) == 30 and max(sleeps) <= 10


# --------------------------------------------------------------------------- #
# llm-sleep-soak
# --------------------------------------------------------------------------- #


def test_soak_parse_args() -> None:
    cfg = sleep_soak.parse_args(["--cycles", "5", "--period", "2m", "--gap", "0", "--watchdog", "--json"])
    assert cfg.cycles == 5 and cfg.period == 120 and cfg.gap == 0 and cfg.watchdog and cfg.as_json
    with pytest.raises(SystemExit):
        sleep_soak.parse_args(["--cycles", "0"])
    with pytest.raises(SystemExit):
        sleep_soak.parse_args(["--period", "abc"])
    with pytest.raises(SystemExit):
        sleep_soak.parse_args(["--bogus"])
    with pytest.raises(SystemExit):
        sleep_soak.parse_args(["--help"])


def test_parse_seconds() -> None:
    assert sleep_soak.parse_seconds("", 7) == 7
    assert sleep_soak.parse_seconds("90s", 0) == 90
    assert sleep_soak.parse_seconds("2m", 0) == 120
    assert sleep_soak.parse_seconds("1h", 0) == 3600
    assert sleep_soak.parse_seconds("bad", 0) is None
    assert sleep_soak.parse_seconds("-5", 0) is None


def test_soak_summarize() -> None:
    results = [
        sleep_soak.CycleResult(1, 2000, True, 2000, 0, True, "ok"),
        sleep_soak.CycleResult(2, 3000, True, 3900, 900, False, "ok"),
        sleep_soak.CycleResult(3, 4000, True, 4001, 1, True, "ok", resume_errors=["xHC error in resume"]),
    ]
    s = sleep_soak.summarize(results, prior_incomplete=0)
    assert s["cycles"] == 3 and s["suspended"] == 3 and s["unreliable"] == 1
    assert s["with_resume_errors"] == 1 and s["all_reliable"] is False and s["max_abs_drift"] == 900
    assert sleep_soak.summarize([sleep_soak.CycleResult(1, 2000, True, 2000, 0, True, "ok")], 0)["all_reliable"] is True
    assert sleep_soak.summarize([], prior_incomplete=2)["all_reliable"] is False


def test_soak_run_cycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = {"LLM_USAGE_NOW_EPOCH": "1000"}
    logs = common.setup_run_logs(tmp_path / "logs", "soak")
    monkeypatch.setattr(common, "suspend_with_wake", lambda *a, **k: common.SuspendOutcome(True, 1122, 1120, 2, True, "systemd-timer", "ok"))
    monkeypatch.setattr(sleep_soak, "scrape_resume_errors", lambda since, e=None: [])
    cfg = sleep_soak.SoakConfig(period=120)
    result = sleep_soak.run_cycle(cfg, logs, 1, env)
    assert result.suspended and result.reliable and result.drift_seconds == 2 and result.ok


def test_soak_scrape_resume_errors_no_journalctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    assert sleep_soak.scrape_resume_errors(1000, {}) == []


def test_soak_scrape_resume_errors_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: True)
    out = "boot ok\nxhci_hcd: xHC error in resume\nall good\nhung_task: blocked\n"
    # scrape_resume_errors does `import subprocess` internally; that resolves to
    # this same module object, so patching subprocess.run here covers it.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc(0, out))
    hits = sleep_soak.scrape_resume_errors(1000, {})
    assert any("xhc error in resume" in h.lower() for h in hits)
    assert any("hung_task" in h.lower() for h in hits)


def test_soak_main_simulated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "1")
    monkeypatch.setenv("LLM_TOOLS_NO_INHIBIT", "1")
    monkeypatch.setattr(sleep_soak.time, "sleep", lambda s: None)
    rc = sleep_soak.main(["--cycles", "3", "--period", "5s", "--gap", "0", "--json"])
    assert rc == 0  # simulated cycles all "ok"


def test_soak_main_no_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", raising=False)
    monkeypatch.setattr(common, "have_cmd", lambda name: False)
    rc = sleep_soak.main(["--cycles", "2"])
    assert rc == 2


def test_soak_main_unreliable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LLM_TOOLS_NO_INHIBIT", "1")
    monkeypatch.setattr(common, "power_backend", lambda e=None: "systemd")
    monkeypatch.setattr(sleep_soak.time, "sleep", lambda s: None)
    monkeypatch.setattr(common, "suspend_with_wake", lambda *a, **k: common.SuspendOutcome(True, 9999, 9000, 999, False, "systemd-timer", "ok"))
    monkeypatch.setattr(sleep_soak, "scrape_resume_errors", lambda since, e=None: [])
    rc = sleep_soak.main(["--cycles", "2", "--gap", "0"])
    assert rc == 1  # an unreliable wake fails the soak
