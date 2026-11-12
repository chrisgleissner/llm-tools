"""Stoppable, read-only capacity observation for long-running Bridge missions."""

from __future__ import annotations

import json
import os
import sqlite3
from functools import lru_cache
from contextlib import nullcontext
from pathlib import Path
import threading
import time
from typing import Any, Callable, cast

from . import bridge_retention
from .bridge_store import BridgeStore, utc_now


@lru_cache(maxsize=256)
def _sample_forecast(label: str, scope: str, remaining: float, reset: object,
                     cache_root: str, time_bucket: int) -> str | None:
    """Reuse unchanged forecasts for at most 30 seconds, with a bounded cache.

    Reset, cache root and time bucket form part of the key. A quota change
    always creates a new observation; rapid identical iterations do not bloat
    shared history or reparse it thousands of times.
    """
    from . import common
    common.log_usage_sample(label, scope, remaining)
    estimate = common.estimate_remaining_time_from_log(label, scope, remaining)
    return None if estimate in {"", "-"} else estimate


_VOLATILE_OBSERVATION_KEYS = frozenset({"observed_at", "estimated_remaining"})
PRUNE_INTERVAL_SECONDS = 900
PRUNE_BUDGET_SECONDS = 0.5
PRUNE_RETRY_SECONDS = 0.25
# Retention that happens on most passes is audited at most this often (counts accumulate in between).
RETENTION_AUDIT_INTERVAL_SECONDS = 3600.0
_ROUTINE_RETENTION_CATEGORIES = frozenset({"events", "capacity_refreshes"})
# A background observation reuses a provider snapshot another Bridge process
# read within this many seconds (see common.shared_usage_reads), so N missions
# cause about one live read per provider per minute instead of N.
SHARED_READ_MAX_AGE_SECONDS = 55.0
_WAIT_OR_SELECTION_KINDS = ("provider-wait", "provider-selection")


def observation_signature(value: Any) -> str:
    """A capacity observation without its timestamp and time-dependent forecast text, as canonical JSON.

    Two observations with the same signature describe the same capacity state.
    """
    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: clean(inner) for key, inner in item.items() if key not in _VOLATILE_OBSERVATION_KEYS}
        if isinstance(item, list):
            return [clean(inner) for inner in item]
        return item

    return json.dumps(clean(value), sort_keys=True, separators=(",", ":"), default=str)


def _process_identity(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = raw[raw.rfind(")") + 2:].split()
        if not tail or tail[0] == "Z" or len(tail) <= 19:
            return ""
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return f"{boot_id}:{tail[19]}" if boot_id else ""
    except (OSError, ValueError):
        return ""


def _safe_decision(selection: dict[str, Any]) -> dict[str, Any]:
    from . import common

    def scalar(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key in keys:
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, str):
                output[key] = value[:256]
            elif isinstance(value, (int, float, bool, type(None))):
                output[key] = value
        return output

    decisions: list[dict[str, Any]] = []
    for decision in selection.get("decisions", []) if isinstance(selection.get("decisions"), list) else []:
        if not isinstance(decision, dict):
            continue
        item = scalar(decision, ("provider", "capacity_provider", "usage_history_provider", "route", "selected_model", "usable", "reason", "wait_until"))
        item["windows"] = [
            scalar(window, ("name", "kind", "remaining", "remaining_percent", "remaining_amount", "currency", "reset_epoch", "reason"))
            for window in decision.get("windows", [])[:8]
            if isinstance(window, dict)
        ] if isinstance(decision.get("windows"), list) else []
        for window in item["windows"]:
            if "remaining_percent" not in window:
                window["remaining_percent"] = window.get("remaining")
            remaining = window.get("remaining_percent")
            if window.get("kind") != "reset_window" or not isinstance(remaining, (int, float)):
                continue
            # The same logged samples and confidence rules as llm-usage.
            # Model-specific histories are never silently replaced with the
            # aggregate history of a different capacity pool.
            provider = str(item.get("capacity_provider") or item.get("provider", ""))
            label = str(item.get("usage_history_provider") or common.usage_history_provider(provider))
            window["estimated_remaining"] = _sample_forecast(label, str(window.get("name", "")), remaining,
                window.get("reset_epoch"), os.fspath(common.usage_cache_dir()), common.now_epoch()//30)
        decisions.append(item)
    common.prune_usage_log()
    selected = cast("dict[str, Any]", selection.get("decision") if isinstance(selection.get("decision"), dict) else {})
    all_rate_limited = bool(selection.get("all_rate_limited"))
    eligibility = [
        int(item["wait_until"])
        for item in decisions
        if item.get("usable") is not True and isinstance(item.get("wait_until"), int)
    ]
    return {
        "provider": str(selection.get("provider") or selected.get("provider") or ""),
        "route": str(selection.get("route") or ""),
        "selected_model": str(selection.get("selected_model") or selected.get("selected_model") or ""),
        "rotation_reason": str(selection.get("rotation_reason") or ""),
        "all_rate_limited": all_rate_limited,
        "capacity_source": "Ralph selection; provider source remains provider-specific",
        "freshness": "selection-read; shared provider cache is capped at 55 seconds",
        "next_eligibility_epoch": min(eligibility) if all_rate_limited and eligibility else None,
        "decision": scalar(selected, ("usable", "reason", "wait_until")),
        "decisions": decisions,
        "observed_at": utc_now(),
        **common.diagnostic_clock_fields(),
    }


def refresh_capacity_observation(mission: dict[str, Any], *, fresh: bool = False) -> dict[str, Any]:
    """Ask the existing Ralph selection path for read-only usage decisions."""
    from . import common, config as toolconfig, ralph_robin

    config = mission.get("config", {})
    raw_conf = toolconfig.load_config()
    merged = toolconfig.merged_tool_config(raw_conf, "ralph")
    cfg = ralph_robin.RalphConfig()
    cfg.scope = str(merged.get("scope") or "auto")
    cfg.min_remaining = str(merged.get("min_remaining") or "1")
    cfg.poll_interval = str(merged.get("poll_interval") or "60")
    cfg.providers_spec = str(config.get("providers", "claude,codex,opencode"))
    cfg.routes_spec = str(config.get("routes", ""))
    cfg.providers = ralph_robin.parse_providers(cfg.providers_spec) if not cfg.routes_spec else []
    cfg.routes = ralph_robin.parse_routes_spec(cfg.routes_spec) if cfg.routes_spec else []
    if cfg.routes:
        from .routes import route_or_implicit_provider

        for route_id in cfg.routes:
            policy = route_or_implicit_provider(raw_conf, route_id)
            if policy is None:
                raise ValueError(f"unknown route in mission refresh configuration: {route_id}")
            cfg.route_policies[route_id] = policy
    ralph_robin.resolve_policies(cfg, raw_conf)
    null_path = Path("/dev/null")
    null = common.RunLogs(run_dir=null_path, text_log=null_path, event_log=null_path)
    read_scope = nullcontext() if fresh else common.shared_usage_reads(SHARED_READ_MAX_AGE_SECONDS)
    with read_scope:
        selection = ralph_robin.select_provider(cfg, null, 0, set())
    return _safe_decision(selection)


class ProgressCounter:
    """Count of passes the controller's main loop has made (INT-016).

    The heartbeat comes from the refresh thread, so a fresh heartbeat alone does not show that the main loop is
    running. The heartbeat carries this counter as ``main_loop_progress``. It changes on every pass of
    ``before_iteration`` (the wait for the next task) and about every 0.5 s while a verifier runs. It does not
    change while a provider run is in progress, because that run is made inline by the scheduler, which does
    not report to the controller: a value that stays the same across heartbeats therefore means either a
    provider run in progress or a blocked loop, and the mission's operation status tells them apart.
    """

    def __init__(self) -> None:
        self.value = 0

    def tick(self) -> None:
        self.value += 1


class CapacityRefreshWorker:
    """One controller-owned refresh loop; never overlaps its own refresh work."""

    def __init__(
        self,
        mission_id: str,
        database: str | os.PathLike[str],
        *,
        interval_seconds: int = 60,
        heartbeat_seconds: int = 5,
        initial_refresh_delay_seconds: float | None = None,
        prune_interval_seconds: float = PRUNE_INTERVAL_SECONDS,
        selector: Callable[[dict[str, Any]], dict[str, Any]] = refresh_capacity_observation,
        progress: ProgressCounter | None = None,
    ) -> None:
        self.progress = progress
        if interval_seconds < 5 or heartbeat_seconds < 1:
            raise ValueError("capacity refresh interval must be >=5s and heartbeat interval >=1s")
        self.mission_id = mission_id
        self.database = os.fspath(database)
        self.interval_seconds = int(interval_seconds)
        self.heartbeat_seconds = int(heartbeat_seconds)
        initial_delay = interval_seconds if initial_refresh_delay_seconds is None else float(initial_refresh_delay_seconds)
        if initial_delay < 0 or initial_delay > 3600:
            raise ValueError("initial refresh delay must be between 0 and 3600s")
        self.initial_refresh_delay_seconds = initial_delay
        self.selector = selector
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._store: BridgeStore | None = None
        self.prune_interval_seconds = float(prune_interval_seconds)
        self._last_refresh: tuple[str, int] | None = None
        self._refresh_loaded = False
        self._pruned: dict[str, int] = {}
        self._retention_deleted: dict[str, int] = {}
        self._retention_missions: dict[str, dict[str, int]] = {}
        self._retention_cutoffs: dict[str, str] = {}
        self._retention_record_after = 0.0
        self._last_wake_plan: tuple[int | None, bool] | None = None
        self._last_wake_signature: tuple[Any, ...] | None = None
        self._last_clock_pair: tuple[int, tuple[float, float] | None] | None = None
        self._thread = threading.Thread(target=self._run, name=f"bridge-refresh-{mission_id[:8]}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._write_lock:
            try:
                from . import common
                from .bridge_power import shared_wake_owners, update_wake_request

                arm = update_wake_request(self.mission_id, None)
                owners = shared_wake_owners()
                with BridgeStore(self.database) as store:
                    store.record_runtime_event(self.mission_id, "capacity-wake-plan", {
                        "action": "owner-released",
                        "owner": self.mission_id,
                        "owners": owners,
                        "target_epoch": arm.target_epoch,
                        "method": arm.method,
                        "unit": arm.unit,
                        "armed": arm.armed,
                        "confirmed": arm.confirmed,
                        "result": "retained-prior" if "retained prior" in arm.detail else "armed" if arm.armed else "unavailable",
                        **common.diagnostic_clock_fields(),
                    })
            except (OSError, ValueError):
                pass
        self.flush_retention_audit()
        # Capacity readers have bounded subprocess/network timeouts but can be
        # slower than a stop request. They are daemonized; the stop flag keeps
        # a late result from reaching SQLite after this method returns.
        self._thread.join(timeout=2)

    def _connection(self) -> BridgeStore:
        """The worker thread's one long-lived connection, opened on first use.

        Opening a store per heartbeat (two per beat before) repeated the connection
        setup every few seconds. SQLite connections stay on the thread that
        created them, and only the worker thread calls this.
        """
        if self._store is None:
            self._store = BridgeStore(self.database)
        return self._store

    def _discard_connection(self) -> None:
        store, self._store = self._store, None
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    def _record(self, kind: str, data: dict[str, Any]) -> None:
        with self._write_lock:
            if self._stop.is_set():
                return
            try:
                self._connection().record_runtime_event(self.mission_id, kind, data)
            except sqlite3.Error:
                # Reopen on the next beat rather than keep a broken connection.
                self._discard_connection()
                raise

    def _record_refresh(self, observation: dict[str, Any]) -> None:
        """Record a capacity observation, or bring the previous row up to date when nothing changed.

        An unchanged observation (same signature) only advances the previous row's
        payload and time, so the console still shows how fresh it is without a new
        row every minute. A new row is written whenever the state changed or a
        provider-wait/provider-selection event came after the previous row, because
        the monitor reads the order of those events.
        """
        with self._write_lock:
            if self._stop.is_set():
                return
            try:
                store = self._connection()
                if not self._refresh_loaded:
                    self._refresh_loaded = True
                    latest = store.latest_event(self.mission_id, "capacity-refresh")
                    if latest is not None:
                        self._last_refresh = (observation_signature(latest["data"]), int(latest["id"]))
                signature = observation_signature(observation)
                last = self._last_refresh
                if (
                    last is not None
                    and last[0] == signature
                    and store.latest_event_id(self.mission_id, _WAIT_OR_SELECTION_KINDS) < last[1]
                    and store.refresh_runtime_event(last[1], observation)
                ):
                    return
                self._last_refresh = (signature, store.record_runtime_event(self.mission_id, "capacity-refresh", observation))
            except sqlite3.Error:
                self._discard_connection()
                raise

    def _retention_policy(self) -> bridge_retention.RetentionPolicy:
        """The ``[bridge.retention]`` policy; the defaults when the config file cannot be read or is invalid."""
        from . import config as toolconfig

        try:
            return bridge_retention.policy_from_config(toolconfig.load_config())
        except (SystemExit, OSError, ValueError):
            return bridge_retention.RetentionPolicy()

    def flush_retention_audit(self) -> None:
        """Record retention work that is still waiting for its hourly audit event (called when the worker stops)."""
        with self._write_lock:
            try:
                if self._retention_deleted:
                    with BridgeStore(self.database) as store:
                        self._record_retention(store)
            except (OSError, ValueError, sqlite3.Error):
                pass

    def _record_retention(self, store: BridgeStore) -> None:
        """Write the audit events for the retention work accumulated since the last record, and reset the counts.

        Routine pruning (old diagnostic events, thinned capacity refreshes) happens on most passes, so it is
        recorded at most once per ``RETENTION_AUDIT_INTERVAL_SECONDS``; the counts keep accumulating in between
        and are flushed when the worker stops. Any other category is recorded on the pass that removed it.
        """
        if not self._retention_deleted:
            return
        for mission_id, counts in self._retention_missions.items():
            if mission_id != self.mission_id:
                store.record_retention_pruned(mission_id, {"deleted": counts, "cutoffs": self._retention_cutoffs})
        store.record_retention_pruned(self.mission_id, {
            "deleted": self._retention_deleted,
            "missions": self._retention_missions,
            "mission_count": len(self._retention_missions),
            "cutoffs": self._retention_cutoffs,
        })
        self._retention_deleted, self._retention_missions = {}, {}
        self._retention_record_after = time.monotonic() + RETENTION_AUDIT_INTERVAL_SECONDS

    def _prune_pass(self) -> bool:
        """Delete one budgeted slice of expired rows; ``True`` when the pass is finished.

        The pass first bounds heartbeats and capacity refreshes (INT-011), then applies the data-retention
        policy to every mission and to the migration backups. A finished pass that removed anything leaves
        one ``events-pruned`` audit event on this mission with the totals of the whole pass, and one on each
        other mission that lost rows or files (routine pruning is audited at most hourly; see ``_record_retention``).
        """
        with self._write_lock:
            if self._stop.is_set():
                return True
            try:
                store = self._connection()
                result = store.prune_plumbing_events(self.mission_id, budget_seconds=PRUNE_BUDGET_SECONDS)
                for kind, count in result["deleted"].items():
                    self._pruned[kind] = self._pruned.get(kind, 0) + int(count)
                if not result["complete"]:
                    return False
                policy = self._retention_policy()
                retention = bridge_retention.run_pass(store, policy=policy, budget_seconds=PRUNE_BUDGET_SECONDS)
                for mission_id, counts in retention["missions"].items():
                    bucket = self._retention_missions.setdefault(mission_id, {})
                    for category, count in counts.items():
                        bucket[category] = bucket.get(category, 0) + int(count)
                for category, count in retention["deleted"].items():
                    self._retention_deleted[category] = self._retention_deleted.get(category, 0) + int(count)
                self._retention_cutoffs = retention["cutoffs"]
                if not retention["complete"]:
                    return False
                if self._pruned:
                    store.record_events_pruned(self.mission_id, {**result, "deleted": self._pruned})
                    self._pruned = {}
                if self._retention_deleted:
                    routine = set(self._retention_deleted) <= _ROUTINE_RETENTION_CATEGORIES
                    if not routine or time.monotonic() >= self._retention_record_after:
                        self._record_retention(store)
                return True
            except sqlite3.Error:
                self._discard_connection()
                raise

    def _run(self) -> None:
        try:
            self._loop()
        finally:
            self._discard_connection()

    def _loop(self) -> None:
        # Ralph already refreshes capacity as it starts/selects a station.
        # The worker only supplements long-running waits after that initial
        # foreground observation, avoiding a duplicate burst of API calls.
        next_refresh = time.monotonic() + self.initial_refresh_delay_seconds
        next_heartbeat = 0.0
        next_prune = 0.0
        pid = os.getpid()
        process_start = _process_identity(pid)
        while not self._stop.is_set():
            now = time.monotonic()
            self._observe_sleep_resume()
            if now >= next_heartbeat:
                try:
                    mission = self._connection().get_mission(self.mission_id)
                    self._record("controller-heartbeat", {
                        "pid": pid,
                        "process_start": process_start,
                        "controller_state": "ACTIVE",
                        "owner_lock_held": True,
                        "desired_mission_status": mission["status"],
                        **({"main_loop_progress": self.progress.value} if self.progress is not None else {}),
                        "observed_at": utc_now(),
                    })
                except Exception as exc:
                    self._record("controller-heartbeat-error", {"reason": type(exc).__name__, "observed_at": utc_now()})
                next_heartbeat = time.monotonic() + self.heartbeat_seconds
            if self._stop.is_set():
                break
            if now >= next_refresh:
                try:
                    mission = self._connection().get_mission(self.mission_id)
                    observation = self.selector(mission)
                    self._record_refresh(observation)
                    self._refresh_manual_wake(mission, observation)
                except Exception as exc:
                    self._record("capacity-refresh-error", {
                        "reason": type(exc).__name__,
                        "observed_at": utc_now(),
                    })
                next_refresh = time.monotonic() + self.interval_seconds
            if now >= next_prune and not self._stop.is_set():
                try:
                    finished = self._prune_pass()
                except Exception:
                    finished = True  # pruning is housekeeping; try again at the next interval
                next_prune = time.monotonic() + (self.prune_interval_seconds if finished else PRUNE_RETRY_SECONDS)
            timeout = max(0.05, min(next_refresh - time.monotonic(), next_heartbeat - time.monotonic(), next_prune - time.monotonic()))
            self._stop.wait(timeout)

    def _refresh_manual_wake(self, mission: dict[str, Any], observation: dict[str, Any]) -> None:
        """Pre-arm the shared wake while a paused or detached opted-in mission waits."""
        from . import bridge_power, common

        with self._write_lock:
            if self._stop.is_set():
                return
            raw_config = mission.get("config")
            config = raw_config if isinstance(raw_config, dict) else {}
            waiting = (
                mission.get("status") in {"RUNNING", "PAUSED"}
                and config.get("suspend_when_rate_limited") is True
            )
            target = bridge_power.recovery_target(observation) if waiting else None
            arm = bridge_power.update_wake_request(
                self.mission_id,
                target,
                interval_seconds=self.interval_seconds,
            )
            earliest = bridge_power.shared_wake_target()
            arm_covers_earliest = bool(arm.armed and arm.target_epoch is not None and earliest is not None and arm.target_epoch <= earliest)
            armed_target = arm.target_epoch if arm_covers_earliest else None
            state = (armed_target, arm_covers_earliest)
            owners = bridge_power.shared_wake_owners()
            signature = (target, state, arm.method, arm.unit, arm.confirmed, arm.detail[:128], tuple(owners))
            if signature == self._last_wake_signature:
                return
            self._last_wake_signature = signature
            self._last_wake_plan = state
            store = self._connection()
            if target is not None:
                kind = "capacity-wake-armed" if arm_covers_earliest else "capacity-wake-unavailable"
                store.record_runtime_event(self.mission_id, kind, {
                    "target_epoch": armed_target or target,
                    "mission_target_epoch": target,
                    "method": arm.method,
                    "confirmed": arm.confirmed,
                    "provider": observation.get("provider"),
                    "route": observation.get("route"),
                    "reason": "shared-wake-retained-prior" if "retained prior" in arm.detail else "wake-arm-failed" if not arm_covers_earliest else "",
                })
            store.record_runtime_event(self.mission_id, "capacity-wake-plan", {
                "action": "armed" if arm_covers_earliest else "unavailable" if target is not None else "released-or-no-target",
                "owner": self.mission_id,
                "owners": owners,
                "target_epoch": armed_target or target,
                "mission_target_epoch": target,
                "method": arm.method,
                "unit": arm.unit,
                "armed": arm.armed,
                "confirmed": arm.confirmed,
                "provider": observation.get("provider"),
                "route": observation.get("route"),
                "result": "retained-prior" if "retained prior" in arm.detail else "armed" if arm.armed else "unavailable",
                **common.diagnostic_clock_fields(),
            })

    def _observe_sleep_resume(self) -> None:
        """Correlate a real suspend gap with the last confirmed Bridge wake plan."""
        from . import common

        wall = common.now_epoch()
        suspend_clock = common.suspend_clock_sample()
        prior = self._last_clock_pair
        self._last_clock_pair = (wall, suspend_clock)
        if prior is None or self._last_wake_plan is None:
            return
        target, armed = self._last_wake_plan
        if not armed or target is None:
            return
        suspend_gap = common.suspend_clock_gap_seconds(prior[1], suspend_clock)
        if suspend_gap is None:
            if prior[1] is not None:
                self._record("capacity-wake-resume-unverified", {
                    "actual_sleep": None,
                    "target_epoch": target,
                    "reason": "suspend-inclusive clock unavailable",
                })
            return
        if suspend_gap < common.SUSPEND_OBSERVED_MIN_GAP_SECONDS:
            return
        drift = wall - target
        self._record("capacity-wake-resumed", {
            "actual_sleep": True,
            "suspend_seconds": int(suspend_gap),
            "target_epoch": target,
            "woke_epoch": wall,
            "drift_seconds": int(drift),
            "reliable": abs(drift) <= common.suspend_drift_tolerance(),
            "method": "bridge-wake-plan",
        })


__all__ = ["CapacityRefreshWorker", "refresh_capacity_observation"]
