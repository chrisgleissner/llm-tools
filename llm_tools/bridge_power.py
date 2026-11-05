"""Safe Bridge capacity wake planning and explicit suspend control."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from . import common
from .bridge_store import BridgeStore, store_path

_WAKE_UNIT = "llm-bridge-capacity-wake"
_RECOVERABLE_REASONS = frozenset({"rate-limited", "budget-exhausted"})
_ACTIVE_OPERATIONS = ("INTENT", "PREPARED", "RUNNING")


def _root(env: dict[str, str] | None = None) -> Path:
    root = common.cache_root(env) / "llm-bridge" / "capacity-wakes"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _request_path(root: Path, mission_id: str) -> Path:
    digest = hashlib.sha256(mission_id.encode("utf-8")).hexdigest()
    return root / f"{digest}.json"


def recovery_target(observation: dict[str, Any], *, now: int | None = None) -> int | None:
    """Return the earliest actual reset in a completely blocked selection.

    Polling backoffs, opaque routes, missing CLIs and exhausted balances with
    no reported reset are not evidence that credits recover at a known time.
    """
    if observation.get("all_rate_limited") is not True:
        return None
    now = common.now_epoch() if now is None else int(now)
    decisions = observation.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return None
    targets: list[int] = []
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get("usable") is True:
            continue
        reason = decision.get("reason")
        target = decision.get("wait_until")
        if reason not in _RECOVERABLE_REASONS or not isinstance(target, int) or target <= now:
            continue
        windows = decision.get("windows")
        if not isinstance(windows, list):
            continue
        if not any(
            isinstance(window, dict)
            and window.get("kind") == "reset_window"
            and isinstance(window.get("reset_epoch"), int)
            and int(window["reset_epoch"]) == target
            for window in windows
        ):
            # Budget exhaustion is predictable only when its actual budget
            # reset is present, not when wait_until is just a poll interval.
            if reason != "budget-exhausted":
                continue
            if not any(
                isinstance(window, dict)
                and window.get("kind") == "budget"
                and isinstance(window.get("reset_epoch"), int)
                and int(window["reset_epoch"]) == target
                for window in windows
            ):
                continue
        targets.append(target)
    return min(targets) if targets else None


def _read_requests(root: Path, now: int) -> dict[str, dict[str, Any]]:
    requests: dict[str, dict[str, Any]] = {}
    for path in root.glob("*.json"):
        if path.name == "plan.json":
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            owner_value = item["owner"]
            target_value = item["target_epoch"]
            expires_value = item["expires_epoch"]
            if (
                not isinstance(owner_value, str) or not owner_value
                or not isinstance(target_value, int) or isinstance(target_value, bool)
                or not isinstance(expires_value, int) or isinstance(expires_value, bool)
            ):
                raise ValueError("invalid wake request fields")
            owner, target, expires = owner_value, target_value, expires_value
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        if _request_path(root, owner) != path:
            path.unlink(missing_ok=True)
            continue
        if expires <= now or target <= now:
            path.unlink(missing_ok=True)
            continue
        requests[owner] = {"owner": owner, "target_epoch": target, "expires_epoch": expires}
    return requests


def _write_json_atomically(root: Path, name: str, payload: dict[str, Any]) -> None:
    temp = root / f"{name}.tmp"
    path = root / name
    with temp.open("w", encoding="utf-8") as stream:
        os.chmod(temp, 0o600)
        json.dump(payload, stream, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_plan(root: Path, target: int | None, arm: common.WakeArm | None, now: int) -> None:
    payload = {
        "target_epoch": target,
        "method": arm.method if arm else "none",
        "armed": bool(arm and arm.armed),
        "confirmed": bool(arm and arm.confirmed),
        "unit": arm.unit if arm else "",
        "updated_epoch": now,
    }
    _write_json_atomically(root, "plan.json", payload)


def _stored_arm(prior: dict[str, Any]) -> common.WakeArm:
    return common.WakeArm(
        bool(prior.get("armed")), str(prior.get("method") or "none"),
        bool(prior.get("confirmed")), unit=str(prior.get("unit") or ""),
        target_epoch=int(prior["target_epoch"]) if isinstance(prior.get("target_epoch"), int) else None,
    )


def _read_plan(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _reconcile(root: Path, requests: dict[str, dict[str, Any]], now: int, env: dict[str, str]) -> common.WakeArm:
    target = min((int(item["target_epoch"]) for item in requests.values()), default=None)
    if target is None:
        prior = _read_plan(root)
        prior_arm = _stored_arm(prior)
        if prior_arm.armed:
            common.disarm_rtc_wake(prior_arm, env)
        _write_plan(root, None, None, now)
        return common.WakeArm(False, "none", False)
    prior = _read_plan(root)
    if prior.get("target_epoch") == target and prior.get("armed") is True:
        arm = _stored_arm(prior)
        if arm.method == "rtcwake":
            current = common.read_rtc_wakealarm(env)
            if current is not None and abs(current - target) <= common.suspend_drift_tolerance(env):
                return arm
        elif arm.method == "systemd-timer" and arm.unit:
            active = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", f"{arm.unit}.timer"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            if active.returncode == 0:
                return arm
    arm = common.arm_rtc_wake(target, f"{_WAKE_UNIT}-{target}", env)
    if not arm.armed:
        retained = _stored_arm(prior)
        if retained.armed:
            if retained.method == "rtcwake" and retained.target_epoch is not None:
                restored = common.arm_rtc_wake(
                    retained.target_epoch, f"{_WAKE_UNIT}-{retained.target_epoch}", env
                )
                if not restored.armed:
                    return common.WakeArm(False, "none", False, detail="new and prior RTC wake could not be verified")
                _write_plan(root, retained.target_epoch, restored, now)
                return restored
            # A failed later plan must not erase a wake that was already armed.
            return common.WakeArm(
                True, retained.method, retained.confirmed, unit=retained.unit,
                detail=f"new target {target} failed to arm; retained prior wake",
                target_epoch=retained.target_epoch,
            )
        _write_plan(root, target, arm, now)
        return arm
    old_arm = _stored_arm(prior)
    if old_arm.armed and not (old_arm.method == "rtcwake" and arm.method == "rtcwake"):
        common.disarm_rtc_wake(old_arm, env)
    _write_plan(root, target, arm, now)
    return arm


def update_wake_request(
    mission_id: str,
    target_epoch: int | None,
    *,
    interval_seconds: int = 60,
    env: dict[str, str] | None = None,
) -> common.WakeArm:
    """Refresh/remove one mission's request and preserve every other owner."""
    env = dict(os.environ if env is None else env)
    root = _root(env)
    now = common.now_epoch(env)
    with common.wake_planner_lock(env):
        request = _request_path(root, mission_id)
        if target_epoch is None or target_epoch <= now:
            request.unlink(missing_ok=True)
        else:
            payload = {
                "owner": mission_id,
                "target_epoch": int(target_epoch),
                "expires_epoch": int(target_epoch) + max(1800, int(interval_seconds) * 3),
            }
            digest = hashlib.sha256(mission_id.encode("utf-8")).hexdigest()
            _write_json_atomically(root, f"{digest}.json", payload)
        requests = _read_requests(root, now)
        return _reconcile(root, requests, now, env)


def shared_wake_target(env: dict[str, str] | None = None) -> int | None:
    """Read the earliest live request under the same lock used for updates."""
    env = dict(os.environ if env is None else env)
    root = _root(env)
    with common.wake_planner_lock(env):
        requests = _read_requests(root, common.now_epoch(env))
        return min((int(item["target_epoch"]) for item in requests.values()), default=None)


def shared_wake_owners(env: dict[str, str] | None = None) -> list[str]:
    """Return bounded private mission ids currently owning a wake request."""
    env = dict(os.environ if env is None else env)
    root = _root(env)
    with common.wake_planner_lock(env):
        requests = _read_requests(root, common.now_epoch(env))
        return sorted(requests)[:64]


def reconcile_shared_wake(env: dict[str, str] | None = None) -> common.WakeArm:
    """Restore the earliest live owner request after a suspend cycle changes the RTC wake."""
    env = dict(os.environ if env is None else env)
    root = _root(env)
    now = common.now_epoch(env)
    with common.wake_planner_lock(env):
        requests = _read_requests(root, now)
        return _reconcile(root, requests, now, env)


def _os_sleep_blocker() -> str | None:
    """Return a blocker name, empty when clear, or None when it cannot be checked."""
    inhibitors: list[tuple[str, str, str]] | None = None
    if common.have_cmd("busctl"):
        try:
            result = subprocess.run(
                [
                    "busctl", "--json=short", "--system", "--timeout=5s", "call",
                    "org.freedesktop.login1", "/org/freedesktop/login1",
                    "org.freedesktop.login1.Manager", "ListInhibitors",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=6,
            )
            if result.returncode == 0:
                payload = json.loads(result.stdout)
                data = payload.get("data") if isinstance(payload, dict) and payload.get("type") == "a(ssssuu)" else None
                rows = data[0] if isinstance(data, list) and len(data) == 1 else None
                if isinstance(rows, list) and all(
                    isinstance(row, list) and len(row) == 6 and all(isinstance(field, str) for field in row[:4])
                    for row in rows
                ):
                    inhibitors = [(str(row[1]), str(row[0]), str(row[3])) for row in rows]
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            pass
    if inhibitors is None and common.have_cmd("systemd-inhibit"):
        try:
            result = subprocess.run(
                ["systemd-inhibit", "--list", "--no-pager"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
                env={**os.environ, "LC_ALL": "C", "LANG": "C", "SYSTEMD_COLORS": "0"},
            )
            if result.returncode != 0:
                return None
            lines = result.stdout.splitlines()
            header = lines[0] if lines else ""
            what_start = header.find("WHAT")
            why_start = header.find("WHY", what_start + 4) if what_start >= 0 else -1
            mode_start = header.find("MODE", why_start + 3) if why_start >= 0 else -1
            who_start = header.find("WHO")
            uid_start = header.find("UID", who_start + 3) if who_start >= 0 else -1
            if min(what_start, why_start, mode_start, who_start, uid_start) < 0:
                if any("no inhibitors" in line.lower() for line in lines):
                    inhibitors = []
                else:
                    return None
            else:
                inhibitors = []
                for line in lines[1:]:
                    mode_parts = line[mode_start:].strip().split(maxsplit=1)
                    if not mode_parts:
                        continue
                    inhibitors.append((
                        line[who_start:uid_start].strip(),
                        ":".join(line[what_start:why_start].strip().split(":")),
                        mode_parts[0],
                    ))
        except (OSError, subprocess.TimeoutExpired):
            return None
    if inhibitors is None:
        return "" if not common.have_cmd("systemctl") else None
    for who, what, mode in inhibitors:
        if "sleep" in what.split(":") and mode in {"block", "block-weak"}:
            return who or "unknown owner"
    return ""


def suspend_block_reason(store: BridgeStore, env: dict[str, str] | None = None) -> str:
    """Explain why host sleep would overlap owned work or an OS sleep blocker."""
    env = dict(os.environ if env is None else env)
    marks = ",".join("?" for _ in _ACTIVE_OPERATIONS)
    active = store.db.execute(
        f"SELECT mission_id, id, status FROM operations WHERE status IN ({marks}) LIMIT 1",
        _ACTIVE_OPERATIONS,
    ).fetchone()
    if active is not None:
        return f"operation {active['id']} is {active['status'].lower()} in mission {active['mission_id'][:8]}"
    uncertain = store.db.execute(
        "SELECT id, process_pid, process_start FROM operations WHERE status='UNCERTAIN' AND process_pid IS NOT NULL"
    ).fetchall()
    from .bridge_runtime import _process_start_token
    for operation in uncertain:
        if operation["process_start"] and _process_start_token(int(operation["process_pid"])) == str(operation["process_start"]):
            return f"uncertain operation {operation['id']} still has a live provider process"
    managed = store.db.execute(
        "SELECT id, kind, status, process_pid, process_start FROM managed_processes WHERE status IN ('STARTING','RUNNING','UNCERTAIN')"
    ).fetchall()
    for process in managed:
        if process["status"] == "STARTING":
            return f"managed {process['kind']} process {process['id']} is {process['status'].lower()}"
        process_pid = process["process_pid"]
        process_start = str(process["process_start"] or "")
        if process["status"] == "RUNNING" and (process_pid is None or not process_start):
            return f"managed {process['kind']} process {process['id']} has no verifiable process identity"
        if process_pid is not None and process_start and _process_start_token(int(process_pid)) == process_start:
            return f"managed {process['kind']} process {process['id']} is still running"
    blocker = _os_sleep_blocker()
    if blocker is None:
        return "cannot verify OS sleep inhibitors"
    if blocker:
        return f"OS sleep is blocked by {blocker}"
    return ""


_safe_to_suspend = suspend_block_reason


def _refresh(mission: dict[str, Any]) -> dict[str, Any]:
    from .bridge_refresh import refresh_capacity_observation

    return refresh_capacity_observation(mission, fresh=True)


def suspend_until_credits(
    mission_id: str,
    *,
    database: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Refresh selected capacity, reject unsafe plans, then sleep to its reset."""
    env = dict(os.environ if env is None else env)
    path = Path(database) if database is not None else store_path(env)
    cycle_id = f"bridge-{mission_id}-{int(time.time() * 1000)}"
    with BridgeStore(path) as store:
        mission = store.get_mission(mission_id)
        observation = _refresh(mission)
        target = recovery_target(observation, now=common.now_epoch(env))
        if target is None:
            message = "No known future credit reset is available for the selected capacity."
            store.record_runtime_event(mission_id, "suspend-refused", {"reason": "no-known-reset", "observation": observation})
            return {"ok": False, "status": "refused", "message": message, "reason": "no-known-reset"}
        unsafe = _safe_to_suspend(store, env)
        if unsafe:
            store.record_runtime_event(mission_id, "suspend-refused", {"reason": "unsafe-operation", "detail": unsafe, "target_epoch": target, "cycle_id": cycle_id})
            return {"ok": False, "status": "refused", "message": f"Cannot suspend safely: {unsafe}.", "reason": "unsafe-operation", "target_epoch": target}
    simulated = env.get("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "0") == "1"
    arm = common.WakeArm(False, "simulated", False) if simulated else update_wake_request(mission_id, target, env=env)
    planned_target = target if simulated else shared_wake_target(env)
    with BridgeStore(path) as store:
        store.record_runtime_event(mission_id, "suspend-intent", {
            "cycle_id": cycle_id,
            "target_epoch": planned_target if planned_target is not None else target,
            "mission_target_epoch": target,
            "provider": observation.get("provider"),
            "route": observation.get("route"),
            "decision": observation.get("decision"),
            "wake_armed": bool(arm.armed),
            "wake_confirmed": bool(arm.confirmed),
            "wake_method": arm.method,
            "simulated": simulated,
        })
    if not simulated and not arm.armed:
        result = {
            "ok": False,
            "status": "failed",
            "message": f"Stayed awake: wake could not be armed ({arm.detail or arm.method}).",
            "target_epoch": target,
            "cycle_id": cycle_id,
            "method": arm.method,
            "wake_method": arm.method,
            "wake_armed": bool(arm.armed),
            "wake_confirmed": bool(arm.confirmed),
            "reliable": False,
            "reason": "arm-failed",
            "woke_epoch": None,
            "drift_seconds": None,
            "actual_sleep": False,
            "simulated": simulated,
        }
        with BridgeStore(path) as store:
            store.record_runtime_event(mission_id, "suspend-outcome", result)
        return result
    if planned_target is None:
        result = {
            "ok": False,
            "status": "failed",
            "message": "Stayed awake: the shared wake plan has no future target.",
            "target_epoch": target,
            "cycle_id": cycle_id,
            "method": arm.method,
            "wake_method": arm.method,
            "wake_armed": bool(arm.armed),
            "wake_confirmed": bool(arm.confirmed),
            "reliable": False,
            "reason": "no-shared-target",
            "woke_epoch": None,
            "drift_seconds": None,
            "actual_sleep": False,
            "simulated": simulated,
        }
        with BridgeStore(path) as store:
            store.record_runtime_event(mission_id, "suspend-outcome", result)
        return result

    def final_safety_check() -> str:
        with BridgeStore(path, readonly=True) as store:
            return _safe_to_suspend(store, env)

    outcome = common.suspend_with_wake(
        planned_target,
        who="llm-bridge",
        cycle_id=cycle_id,
        wake_arm=None if simulated else arm,
        pre_suspend_check=None if simulated else final_safety_check,
        env=env,
    )
    reconcile_shared_wake(env)
    result = {
        "ok": bool(outcome.suspended and outcome.reliable),
        "status": "suspended" if outcome.suspended else "unverified" if outcome.sleep_observed is None else "failed",
        "message": (
            "Suspend was requested; actual sleep could not be verified."
            if outcome.sleep_observed is None else
            f"Stayed awake: the final safety check refused suspend ({outcome.detail})."
            if outcome.reason == "pre-suspend-check-failed" else
            f"Woke at {common.format_local_epoch(outcome.woke_epoch or planned_target)}; drift {outcome.drift_seconds}s."
            if outcome.suspended else "Simulated only: the host stayed awake and no wake timer was armed."
            if outcome.reason == "simulated" else f"Stayed awake: wake/suspend failed ({outcome.reason})."
        ),
        "target_epoch": planned_target,
        "cycle_id": cycle_id,
        "method": outcome.method,
        "wake_method": arm.method,
        "wake_armed": bool(arm.armed) if not simulated else False,
        "wake_confirmed": bool(arm.confirmed) if not simulated else False,
        "reliable": outcome.reliable,
        "reason": outcome.reason,
        "detail": outcome.detail,
        "actual_sleep": outcome.sleep_observed,
        "simulated": outcome.reason == "simulated",
        "woke_epoch": outcome.woke_epoch,
        "drift_seconds": outcome.drift_seconds,
    }
    with BridgeStore(path) as store:
        store.record_runtime_event(mission_id, "suspend-outcome", result)
        if outcome.suspended:
            after = _refresh(store.get_mission(mission_id))
            update_wake_request(
                mission_id,
                recovery_target(after, now=common.now_epoch(env)),
                env=env,
            )
            store.record_runtime_event(mission_id, "suspend-work-resumed", {
                "cycle_id": cycle_id,
                "woke_epoch": outcome.woke_epoch,
                "target_epoch": planned_target,
                "drift_seconds": outcome.drift_seconds,
                "reliable": outcome.reliable,
                "method": outcome.method,
                "wake_method": arm.method,
                "wake_armed": bool(arm.armed),
                "wake_confirmed": bool(arm.confirmed),
                "actual_sleep": outcome.sleep_observed,
                "simulated": outcome.reason == "simulated",
                "observation": after,
            })
    return result


__all__ = ["recovery_target", "reconcile_shared_wake", "shared_wake_target", "suspend_block_reason", "suspend_until_credits", "update_wake_request"]
