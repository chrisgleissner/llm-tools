"""Stoppable, read-only capacity observation for long-running Bridge missions."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
import threading
import time
from typing import Any, Callable

from .bridge_store import BridgeStore, default_state_dir, utc_now


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
    selected = selection.get("decision") if isinstance(selection.get("decision"), dict) else {}
    return {
        "provider": str(selection.get("provider") or selected.get("provider") or ""),
        "route": str(selection.get("route") or ""),
        "selected_model": str(selection.get("selected_model") or selected.get("selected_model") or ""),
        "rotation_reason": str(selection.get("rotation_reason") or ""),
        "all_rate_limited": bool(selection.get("all_rate_limited")),
        "decision": scalar(selected, ("usable", "reason", "wait_until")),
        "decisions": decisions,
        "observed_at": utc_now(),
    }


def refresh_capacity_observation(mission: dict[str, Any]) -> dict[str, Any]:
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
    selection = ralph_robin.select_provider(cfg, null, 0, set())
    return _safe_decision(selection)


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
        selector: Callable[[dict[str, Any]], dict[str, Any]] = refresh_capacity_observation,
    ) -> None:
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
        self._thread = threading.Thread(target=self._run, name=f"bridge-refresh-{mission_id[:8]}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Synchronize with any event write already in progress. The next worker
        # write rechecks _stop while holding this lock and cannot occur later.
        with self._write_lock:
            pass
        # Capacity readers have bounded subprocess/network timeouts but can be
        # slower than a stop request. They are daemonized; the stop flag keeps
        # a late result from reaching SQLite after this method returns.
        self._thread.join(timeout=2)

    def _record(self, kind: str, data: dict[str, Any]) -> None:
        with self._write_lock:
            if self._stop.is_set():
                return
            with BridgeStore(self.database) as store:
                store.record_runtime_event(self.mission_id, kind, data)

    def _run(self) -> None:
        # Ralph already refreshes capacity as it starts/selects a station.
        # The worker only supplements long-running waits after that initial
        # foreground observation, avoiding a duplicate burst of API calls.
        next_refresh = time.monotonic() + self.initial_refresh_delay_seconds
        next_heartbeat = 0.0
        pid = os.getpid()
        process_start = _process_identity(pid)
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_heartbeat:
                try:
                    with BridgeStore(self.database) as store:
                        mission = store.get_mission(self.mission_id)
                    self._record("controller-heartbeat", {
                        "pid": pid,
                        "process_start": process_start,
                        "controller_state": "ACTIVE",
                        "owner_lock_held": True,
                        "desired_mission_status": mission["status"],
                        "observed_at": utc_now(),
                    })
                except Exception as exc:
                    self._record("controller-heartbeat-error", {"reason": type(exc).__name__, "observed_at": utc_now()})
                next_heartbeat = time.monotonic() + self.heartbeat_seconds
            if self._stop.is_set():
                break
            if now >= next_refresh:
                try:
                    with BridgeStore(self.database) as store:
                        mission = store.get_mission(self.mission_id)
                    observation = self.selector(mission)
                    self._record("capacity-refresh", observation)
                except Exception as exc:
                    self._record("capacity-refresh-error", {
                        "reason": type(exc).__name__,
                        "observed_at": utc_now(),
                    })
                next_refresh = time.monotonic() + self.interval_seconds
            timeout = max(0.05, min(next_refresh - time.monotonic(), next_heartbeat - time.monotonic()))
            self._stop.wait(timeout)


__all__ = ["CapacityRefreshWorker", "refresh_capacity_observation"]
