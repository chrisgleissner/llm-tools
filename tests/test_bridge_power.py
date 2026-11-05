from __future__ import annotations

import pytest
import json

from llm_tools import bridge_power, common
from llm_tools.bridge_refresh import CapacityRefreshWorker
from llm_tools.bridge_store import BridgeStore


def _blocked(reset: int = 5000, *, reason: str = "rate-limited", kind: str = "reset_window"):
    return {
        "all_rate_limited": True,
        "decisions": [{
            "provider": "codex",
            "usable": False,
            "reason": reason,
            "wait_until": reset,
            "windows": [{"name": "5h", "kind": kind, "reset_epoch": reset}],
        }],
    }


def test_recovery_target_requires_a_real_future_reset() -> None:
    assert bridge_power.recovery_target(_blocked(), now=1000) == 5000
    assert bridge_power.recovery_target(_blocked(), now=5000) is None
    assert bridge_power.recovery_target({**_blocked(), "all_rate_limited": False}, now=1000) is None
    assert bridge_power.recovery_target(_blocked(reason="unknown-route"), now=1000) is None
    assert bridge_power.recovery_target(_blocked(reason="budget-exhausted", kind="balance"), now=1000) is None
    assert bridge_power.recovery_target({"all_rate_limited": True, "decisions": "invalid"}, now=1000) is None
    assert bridge_power.recovery_target({"all_rate_limited": True, "decisions": [{}]}, now=1000) is None
    budget = _blocked(reason="budget-exhausted", kind="budget")
    assert bridge_power.recovery_target(budget, now=1000) == 5000
    assert bridge_power.recovery_target({
        "all_rate_limited": True,
        "decisions": [
            _blocked(7000)["decisions"][0],
            _blocked(3000)["decisions"][0],
            {"usable": True, "reason": "rate-limited", "wait_until": 2000, "windows": []},
        ],
    }, now=1000) == 3000


def test_shared_wake_plan_keeps_earliest_owner_and_uses_common_wake_primitive(tmp_path, monkeypatch) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path), "LLM_USAGE_NOW_EPOCH": "1000"}
    arms: list[int] = []
    disarms: list[str] = []

    def arm(target, who, actual_env):
        arms.append(target)
        assert actual_env == env
        return common.WakeArm(True, "systemd-timer", False, unit=f"{who}.timer", target_epoch=target)

    monkeypatch.setattr(common, "arm_rtc_wake", arm)
    monkeypatch.setattr(common, "disarm_rtc_wake", lambda old, _env: disarms.append(old.unit))
    monkeypatch.setattr(bridge_power.subprocess, "run", lambda *_args, **_kwargs: type("Proc", (), {"returncode": 0})())

    assert bridge_power.update_wake_request("mission-a", 5000, env=env).armed
    assert bridge_power.update_wake_request("mission-b", 4000, env=env).armed
    assert bridge_power.shared_wake_target(env) == 4000
    assert bridge_power.update_wake_request("mission-a", 6000, env=env).armed
    assert bridge_power.shared_wake_target(env) == 4000
    bridge_power.update_wake_request("mission-b", None, env=env)
    assert bridge_power.shared_wake_target(env) == 6000
    assert arms == [5000, 4000, 6000]
    assert len(disarms) == 2


def test_reconcile_after_failed_cycle_restores_other_owners_wake(tmp_path, monkeypatch) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path), "LLM_USAGE_NOW_EPOCH": "1000"}
    armed: list[int] = []
    monkeypatch.setattr(
        common, "arm_rtc_wake",
        lambda target, who, _env: armed.append(target) or common.WakeArm(True, "systemd-timer", False, unit=f"{who}.timer", target_epoch=target),
    )
    monkeypatch.setattr(common, "disarm_rtc_wake", lambda *_args: None)
    monkeypatch.setattr(bridge_power.subprocess, "run", lambda *_args, **_kwargs: type("Proc", (), {"returncode": 1})())
    bridge_power.update_wake_request("other-mission", 4000, env=env)
    # The failed suspend cycle consumed/disarmed the timer but left owner requests intact.
    bridge_power._write_plan(bridge_power._root(env), None, None, 1000)

    arm = bridge_power.reconcile_shared_wake(env)

    assert arm.armed is True
    assert arm.target_epoch == 4000
    assert bridge_power.shared_wake_target(env) == 4000
    assert armed == [4000, 4000]


def test_reconcile_after_elapsed_owner_arms_later_owner_target(tmp_path, monkeypatch) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path), "LLM_USAGE_NOW_EPOCH": "1000"}
    armed: list[int] = []
    inactive = {"value": False}
    monkeypatch.setattr(
        common, "arm_rtc_wake",
        lambda target, who, _env: armed.append(target) or common.WakeArm(True, "systemd-timer", False, unit=f"{who}.timer", target_epoch=target),
    )
    monkeypatch.setattr(common, "disarm_rtc_wake", lambda *_args: None)
    monkeypatch.setattr(bridge_power.subprocess, "run", lambda *_args, **_kwargs: type("Proc", (), {"returncode": int(inactive["value"])})())
    bridge_power.update_wake_request("first-owner", 4000, env=env)
    bridge_power.update_wake_request("later-owner", 6000, env=env)
    inactive["value"] = True
    env["LLM_USAGE_NOW_EPOCH"] = "4000"

    arm = bridge_power.reconcile_shared_wake(env)

    assert arm.armed is True
    assert arm.target_epoch == 6000
    assert bridge_power.shared_wake_target(env) == 6000
    assert armed == [4000, 6000]


def test_failed_earlier_rearm_preserves_existing_wake_for_other_owner(tmp_path, monkeypatch) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path), "LLM_USAGE_NOW_EPOCH": "1000"}
    calls: list[int] = []

    def arm(target, who, _env):
        calls.append(target)
        if target == 4000:
            return common.WakeArm(False, "none", False, detail="timer refused")
        return common.WakeArm(True, "systemd-timer", False, unit=f"{who}.timer", target_epoch=target)

    monkeypatch.setattr(common, "arm_rtc_wake", arm)
    monkeypatch.setattr(common, "disarm_rtc_wake", lambda *_args: pytest.fail("must retain earlier successful wake"))
    monkeypatch.setattr(bridge_power.subprocess, "run", lambda *_args, **_kwargs: type("Proc", (), {"returncode": 0})())

    first = bridge_power.update_wake_request("mission-a", 5000, env=env)
    retained = bridge_power.update_wake_request("mission-b", 4000, env=env)

    assert first.target_epoch == 5000
    assert retained.armed is True
    assert retained.target_epoch == 5000
    assert bridge_power.shared_wake_target(env) == 4000
    assert calls == [5000, 4000]


def test_wake_registry_discards_corrupt_mismatched_and_expired_requests(tmp_path) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path), "LLM_USAGE_NOW_EPOCH": "1000"}
    root = bridge_power._root(env)
    (root / "broken.json").write_text("{", encoding="utf-8")
    (root / "wrong-owner.json").write_text(json.dumps({"owner": "mission-x", "target_epoch": 5000, "expires_epoch": 7000}), encoding="utf-8")
    expired = bridge_power._request_path(root, "mission-expired")
    expired.write_text(json.dumps({"owner": "mission-expired", "target_epoch": 900, "expires_epoch": 2000}), encoding="utf-8")
    current = bridge_power._request_path(root, "mission-current")
    current.write_text(json.dumps({"owner": "mission-current", "target_epoch": 5000, "expires_epoch": 7000}), encoding="utf-8")

    requests = bridge_power._read_requests(root, 1000)

    assert list(requests) == ["mission-current"]
    assert not (root / "broken.json").exists()
    assert not (root / "wrong-owner.json").exists()
    assert not expired.exists()


@pytest.mark.parametrize("bad_plan", ["null", "[]", "1", "\"plan\""])
def test_reconcile_treats_well_formed_non_object_plan_as_missing(tmp_path, monkeypatch, bad_plan) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path)}
    root = bridge_power._root(env)
    (root / "plan.json").write_text(bad_plan, encoding="utf-8")
    monkeypatch.setattr(
        common,
        "arm_rtc_wake",
        lambda target, who, _env: common.WakeArm(True, "systemd-timer", False, unit=who, target_epoch=target),
    )
    monkeypatch.setattr(common, "disarm_rtc_wake", lambda *_args: pytest.fail("invalid plan is not owned"))

    arm = bridge_power._reconcile(root, {"mission": {"target_epoch": 5000}}, 1000, env)

    assert arm.armed and arm.target_epoch == 5000


def test_reconcile_reuses_confirmed_rtc_alarm_without_rearming(tmp_path, monkeypatch) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path)}
    root = bridge_power._root(env)
    bridge_power._write_plan(root, 5000, common.WakeArm(True, "rtcwake", True, target_epoch=5000), 1000)
    monkeypatch.setattr(common, "read_rtc_wakealarm", lambda _env: 5000)
    monkeypatch.setattr(common, "arm_rtc_wake", lambda *_args: pytest.fail("confirmed RTC wake should be reused"))
    monkeypatch.setattr(common, "disarm_rtc_wake", lambda *_args: pytest.fail("confirmed RTC wake should be kept"))

    arm = bridge_power._reconcile(root, {"mission": {"target_epoch": 5000}}, 1000, env)

    assert arm.method == "rtcwake" and arm.target_epoch == 5000


def test_reconcile_restores_or_reports_lost_prior_rtc_on_failed_retarget(tmp_path, monkeypatch) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path)}
    root = bridge_power._root(env)
    bridge_power._write_plan(root, 5000, common.WakeArm(True, "rtcwake", True, target_epoch=5000), 1000)
    calls: list[int] = []

    def arm(target, *_args):
        calls.append(target)
        return common.WakeArm(target == 5000, "rtcwake" if target == 5000 else "none", target == 5000, target_epoch=target)

    monkeypatch.setattr(common, "arm_rtc_wake", arm)
    monkeypatch.setattr(common, "read_rtc_wakealarm", lambda _env: None)

    restored = bridge_power._reconcile(root, {"mission": {"target_epoch": 4000}}, 1000, env)
    assert restored.armed and restored.target_epoch == 5000
    assert calls == [4000, 5000]

    calls.clear()
    monkeypatch.setattr(common, "arm_rtc_wake", lambda target, *_args: calls.append(target) or common.WakeArm(False, "none", False, target_epoch=target))
    lost = bridge_power._reconcile(root, {"mission": {"target_epoch": 3000}}, 1000, env)
    assert lost.armed is False
    assert "prior RTC wake" in lost.detail
    assert calls == [3000, 5000]


def test_suspend_with_wake_refuses_a_stale_prearmed_timer(tmp_path, monkeypatch) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path), "LLM_USAGE_NOW_EPOCH": "1000"}
    calls: list[list[str]] = []
    monkeypatch.setattr(common, "power_backend", lambda _env: "systemd")

    def run(args, **_kwargs):
        calls.append(list(args))
        return type("Proc", (), {"returncode": 1})()

    monkeypatch.setattr(common.subprocess, "run", run)
    outcome = common.suspend_with_wake(
        5000,
        who="test",
        wake_arm=common.WakeArm(True, "systemd-timer", False, unit="old", target_epoch=5000),
        env=env,
    )

    assert outcome.suspended is False
    assert outcome.reason == "arm-failed"
    assert calls == [["systemctl", "--user", "is-active", "--quiet", "old.timer"]]


def test_refresh_worker_prearms_while_mission_is_paused_and_ui_is_irrelevant(tmp_path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
    planned: list[tuple[str, int | None]] = []
    monkeypatch.setattr(
        bridge_power,
        "update_wake_request",
        lambda mission_id, target, **_kwargs: planned.append((mission_id, target)) or common.WakeArm(True, "systemd-timer", False),
    )
    monkeypatch.setattr(bridge_power, "recovery_target", lambda *_args, **_kwargs: 5000)
    worker = CapacityRefreshWorker(mission_id, tmp_path / "bridge.sqlite3", interval_seconds=5)
    worker._refresh_manual_wake(
        {"id": mission_id, "status": "PAUSED", "config": {"suspend_when_rate_limited": True}},
        _blocked(),
    )
    assert planned == [(mission_id, 5000)]


def test_refresh_worker_records_measured_manual_sleep_resume_and_late_wake(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
    worker = CapacityRefreshWorker(mission_id, database, interval_seconds=5)
    worker._last_wake_plan = (1500, True)
    worker._last_clock_pair = (1000, (100.0, 90.0))
    monkeypatch.setattr(common, "now_epoch", lambda *_args, **_kwargs: 2000)
    monkeypatch.setattr(common, "suspend_clock_sample", lambda: (1100.0, 190.0))

    worker._observe_sleep_resume()

    with BridgeStore(database, readonly=True) as store:
        event = store.latest_event(mission_id, "capacity-wake-resumed")
    assert event is not None
    assert event["data"]["actual_sleep"] is True
    assert event["data"]["suspend_seconds"] == 900
    assert event["data"]["drift_seconds"] == 500
    assert event["data"]["reliable"] is False


def test_refresh_worker_does_not_infer_sleep_from_wall_clock_jump(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
    worker = CapacityRefreshWorker(mission_id, database, interval_seconds=5)
    worker._last_wake_plan = (1500, True)
    worker._last_clock_pair = (1000, (100.0, 90.0))
    monkeypatch.setattr(common, "now_epoch", lambda *_args, **_kwargs: 2000)
    monkeypatch.setattr(common, "suspend_clock_sample", lambda: (110.0, 100.0))

    worker._observe_sleep_resume()

    with BridgeStore(database, readonly=True) as store:
        assert store.latest_event(mission_id, "capacity-wake-resumed") is None


def test_refresh_worker_preserves_unknown_sleep_observation(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
    worker = CapacityRefreshWorker(mission_id, database, interval_seconds=5)
    worker._last_wake_plan = (1500, True)
    worker._last_clock_pair = (1000, (100.0, 90.0))
    monkeypatch.setattr(common, "now_epoch", lambda *_args, **_kwargs: 2000)
    monkeypatch.setattr(common, "suspend_clock_sample", lambda: None)

    worker._observe_sleep_resume()

    with BridgeStore(database, readonly=True) as store:
        event = store.latest_event(mission_id, "capacity-wake-resume-unverified")
    assert event is not None
    assert event["data"]["actual_sleep"] is None


def test_explicit_suspend_refuses_unknown_recovery_and_records_mission_event(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
    monkeypatch.setattr(bridge_power, "_refresh", lambda _mission: _blocked(reason="insufficient-balance"))
    monkeypatch.setattr(common, "suspend_with_wake", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must stay awake")))

    result = bridge_power.suspend_until_credits(mission_id, database=database, env={"LLM_USAGE_NOW_EPOCH": "1000"})

    assert result["status"] == "refused"
    assert result["reason"] == "no-known-reset"
    with BridgeStore(database, readonly=True) as store:
        event = store.latest_event(mission_id, "suspend-refused")
    assert event is not None and event["data"]["reason"] == "no-known-reset"


def test_explicit_simulation_never_arms_a_real_wake_timer(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
    monkeypatch.setattr(bridge_power, "_refresh", lambda _mission: _blocked())
    monkeypatch.setattr(bridge_power, "_safe_to_suspend", lambda _store, _env: "")
    monkeypatch.setattr(bridge_power, "update_wake_request", lambda *_args, **_kwargs: pytest.fail("simulation must not arm"))

    result = bridge_power.suspend_until_credits(
        mission_id,
        database=database,
        env={"LLM_USAGE_NOW_EPOCH": "1000", "LLM_SCHEDULER_NO_ACTUAL_SUSPEND": "1"},
    )

    assert result["ok"] is False
    assert result["simulated"] is True
    assert result["actual_sleep"] is False
    assert "no wake timer was armed" in result["message"]


def test_explicit_suspend_records_intent_actual_outcome_and_resume(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
    calls = 0

    def refresh(_mission):
        nonlocal calls
        calls += 1
        return _blocked() if calls == 1 else {"all_rate_limited": False, "decisions": []}

    monkeypatch.setattr(bridge_power, "_refresh", refresh)
    monkeypatch.setattr(bridge_power, "_safe_to_suspend", lambda _store, _env: "")
    monkeypatch.setattr(bridge_power, "update_wake_request", lambda *_args, **_kwargs: common.WakeArm(True, "systemd-timer", False, unit="llm-bridge-capacity-wake-5000", target_epoch=5000))
    monkeypatch.setattr(bridge_power, "shared_wake_target", lambda _env: 5000)
    monkeypatch.setattr(
        common,
        "suspend_with_wake",
        lambda target, **_kwargs: common.SuspendOutcome(True, target + 2, target, 2, True, "systemd-timer", "ok", sleep_observed=True),
    )

    result = bridge_power.suspend_until_credits(mission_id, database=database, env={"LLM_USAGE_NOW_EPOCH": "1000"})

    assert result["ok"] is True
    assert result["target_epoch"] == 5000
    assert result["woke_epoch"] == 5002
    assert result["drift_seconds"] == 2
    assert result["actual_sleep"] is True
    assert result["wake_armed"] is True
    assert result["wake_confirmed"] is False
    with BridgeStore(database, readonly=True) as store:
        events = store.events(mission_id)
        kinds = [event["kind"] for event in events]
    assert kinds == ["mission-created", "suspend-intent", "suspend-outcome", "suspend-work-resumed"]
    assert events[1]["data"]["wake_armed"] is True
    assert events[1]["data"]["wake_confirmed"] is False
    assert events[2]["data"]["woke_epoch"] == 5002
    assert events[2]["data"]["drift_seconds"] == 2


def test_safe_gate_uses_generic_sleep_blockers_not_inhibitor_reason_text(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    header = "WHO                          UID  USER  PID    COMM            WHAT                                                                       WHY                                                        MODE"
    def row(who: str, what: str, why: str, mode: str) -> str:
        return f"{who:<29}{1000:<5}{'chris':<6}{20400:<7}{'service':<16}{what:<75}{why:<59}{mode}"

    monkeypatch.setattr(common, "have_cmd", lambda name: name == "systemd-inhibit")
    with BridgeStore(database) as store:
        store.create_mission("mission", "objective", cwd=tmp_path)
        monkeypatch.setattr(
            bridge_power.subprocess,
            "run",
            lambda *_args, **_kwargs: type("Proc", (), {
                "returncode": 0,
                "stdout": "\n".join((
                    header,
                    row("Screen Locker", "sleep", "Waiting before sleep", "delay"),
                    row("Provider", "idle", "prompt mentions sleep", "block"),
                )),
                "stderr": "",
            })(),
        )
        assert bridge_power._safe_to_suspend(store) == ""

        monkeypatch.setattr(
            bridge_power.subprocess,
            "run",
            lambda *_args, **_kwargs: type("Proc", (), {
                "returncode": 0,
                "stdout": "\n".join((
                    header,
                    row("Device Writer", "sleep", "Device operation in progress", "block"),
                )),
                "stderr": "",
            })(),
        )
        assert bridge_power._safe_to_suspend(store) == "OS sleep is blocked by Device Writer"


def test_safe_gate_refuses_when_os_inhibitor_list_cannot_be_verified(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: name == "systemd-inhibit")
    monkeypatch.setattr(
        bridge_power.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "unavailable"})(),
    )
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        store.create_mission("mission", "objective", cwd=tmp_path)
        assert bridge_power._safe_to_suspend(store) == "cannot verify OS sleep inhibitors"


def test_safe_gate_prefers_structured_login1_inhibitor_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda name: name == "busctl")
    commands: list[list[str]] = []

    def run(args, **_kwargs):
        commands.append(list(args))
        return type("Proc", (), {
            "returncode": 0,
            "stdout": json.dumps({"type": "a(ssssuu)", "data": [[
                ["sleep", "Device Writer", "Device operation in progress", "block", 1000, 123],
            ]]}),
            "stderr": "",
        })()

    monkeypatch.setattr(bridge_power.subprocess, "run", run)
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        store.create_mission("mission", "objective", cwd=tmp_path)
        commands.clear()
        assert bridge_power._safe_to_suspend(store) == "OS sleep is blocked by Device Writer"
    assert commands == [[
        "busctl", "--json=short", "--system", "--timeout=5s", "call",
        "org.freedesktop.login1", "/org/freedesktop/login1",
        "org.freedesktop.login1.Manager", "ListInhibitors",
    ]]


def test_safe_gate_refuses_active_and_live_uncertain_bridge_operations(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
        task_id = store.add_task(mission_id, "work")
        store.set_mission_status(mission_id, "RUNNING")
        operation_id = store.begin_operation(task_id, iteration=1, station="codex")
        monkeypatch.setattr(common, "have_cmd", lambda _name: False)
        reason = bridge_power._safe_to_suspend(store)
        assert "is prepared" in reason

        store.db.execute("UPDATE operations SET status='UNCERTAIN', process_pid=123, process_start='boot:1' WHERE id=?", (operation_id,))
        monkeypatch.setattr("llm_tools.bridge_runtime._process_start_token", lambda pid: "boot:2" if pid == 123 else "")
        assert bridge_power._safe_to_suspend(store) == ""
        monkeypatch.setattr("llm_tools.bridge_runtime._process_start_token", lambda pid: "boot:1" if pid == 123 else "")
        reason = bridge_power._safe_to_suspend(store)
    assert "uncertain operation" in reason


def test_managed_process_safety_uses_typed_status_and_pid_start_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(common, "have_cmd", lambda _name: False)
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
        task_id = store.add_task(mission_id, "managed work")
        store.set_mission_status(mission_id, "RUNNING")
        store.db.execute("UPDATE tasks SET status='VERIFYING' WHERE id=?", (task_id,))
        process_id = store.prepare_managed_process(mission_id, task_id, kind="verifier")
        assert "is starting" in bridge_power._safe_to_suspend(store)

        store.mark_managed_process_running(process_id, process_pid=123, process_start="boot:1")
        monkeypatch.setattr("llm_tools.bridge_runtime._process_start_token", lambda _pid: "boot:2")
        assert bridge_power._safe_to_suspend(store) == ""
        monkeypatch.setattr("llm_tools.bridge_runtime._process_start_token", lambda _pid: "boot:1")
        assert "is still running" in bridge_power._safe_to_suspend(store)

        store.db.execute(
            "UPDATE managed_processes SET status='RUNNING', process_pid=NULL, process_start=NULL WHERE id=?",
            (process_id,),
        )
        assert "no verifiable process identity" in bridge_power._safe_to_suspend(store)
        store.db.execute("UPDATE managed_processes SET status='UNCERTAIN' WHERE id=?", (process_id,))
        assert bridge_power._safe_to_suspend(store) == ""


def test_final_suspend_check_rechecks_owned_work_under_planner_lock(tmp_path, monkeypatch) -> None:
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission_id = store.create_mission("mission", "objective", cwd=tmp_path)["id"]
    monkeypatch.setattr(bridge_power, "_refresh", lambda _mission: _blocked())
    checks = 0

    def safety_check(_store, _env):
        nonlocal checks
        checks += 1
        return "typed operation started" if checks > 1 else ""

    monkeypatch.setattr(bridge_power, "_safe_to_suspend", safety_check)
    monkeypatch.setattr(bridge_power, "update_wake_request", lambda *_args, **_kwargs: common.WakeArm(True, "systemd-timer", False, unit="llm-bridge-capacity-wake-5000", target_epoch=5000))
    monkeypatch.setattr(bridge_power, "shared_wake_target", lambda _env: 5000)
    monkeypatch.setattr(common, "power_backend", lambda _env: "systemd")
    invoked: list[str] = []

    def run(args, **_kwargs):
        invoked.append(" ".join(args))
        return type("Proc", (), {"returncode": 0})()

    monkeypatch.setattr(common.subprocess, "run", run)

    result = bridge_power.suspend_until_credits(mission_id, database=database, env={"LLM_USAGE_NOW_EPOCH": "1000"})

    assert result["reason"] == "pre-suspend-check-failed"
    assert result["detail"] == "typed operation started"
    assert result["actual_sleep"] is False
    assert checks == 2
    assert invoked == ["systemctl --user is-active --quiet llm-bridge-capacity-wake-5000.timer"]
