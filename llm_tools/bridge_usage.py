"""Nonblocking embedded llm-usage dashboard, using its existing Python API."""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping


# Reasons a provider reports when its CLI is not installed. Such providers are left out of the all-providers view.
_NOT_INSTALLED_REASONS = frozenset({"missing-cli", "not-installed", "hidden"})


def _provider_reason(data: Any) -> str:
    return str(data.get("reason") if isinstance(data, dict) else getattr(data, "reason", "") or "")


def _injected_provider_data(env: Mapping[str, str]) -> dict[str, Any] | None:
    """Provider data from LLM_SCHEDULER_USAGE_JSON(_FILE), or None when nothing is injected.

    The controller reads capacity through `common.usage_snapshot_for_provider`, which
    honours that injection. The dashboard has to show the same capacity, so under an
    injection it renders the same snapshots instead of reading the providers again.
    """
    if not (env.get("LLM_SCHEDULER_USAGE_JSON") or env.get("LLM_SCHEDULER_USAGE_JSON_FILE")):
        return None
    from . import common, usage
    from .capacity import ALL_PROVIDERS

    data: dict[str, Any] = {}
    for name in ALL_PROVIDERS:
        snapshot = common.usage_snapshot_for_provider(name, dict(env))
        scopes = common._decision_scopes(snapshot, name)
        available = bool(snapshot.get("available", True))
        reason = str(snapshot.get("reason") or "")
        source = str(snapshot.get("source") or "injected capacity")
        if name == "codex":
            if not available and not scopes:
                data[name] = {"provider": name, "available": False, "reason": reason or "unavailable", "source": source}
                continue
            codex: dict[str, Any] = {"provider": name, "source": source, "rows": []}
            for scope_name, key in (("5h", "five_hour"), ("weekly", "week")):
                scope = next((item for item in scopes if item.get("name") == scope_name), None)
                if scope is not None:
                    remaining = scope.get("remaining_percent")
                    codex[key] = {"resets_at": scope.get("resets_at"), "used": None if remaining is None else 100.0 - remaining}
            data[name] = codex
        elif not available and not scopes:
            data[name] = usage.unavailable_snapshot(name, source, reason or "unavailable")
        else:
            data[name] = usage.ProviderSnapshot(
                provider=name, available=available, reason=reason, source=source,
                selected_model=snapshot.get("selected_model"), scopes=[usage.scope_from_json(item) for item in scopes],
            )
    return data


def collect_usage(rotation: Iterable[str] | None = None) -> dict[str, Any]:
    """The llm-usage dashboard for the embedded view.

    With `rotation` (provider ids of the mission's rotation) the result shows only those
    providers and also carries `all_text` / `all_rows`: every enabled, installed provider,
    built from the same rows.
    """
    from . import usage
    from .bridge_theme import colors_disabled
    cfg = usage.Config()
    cfg.show_remaining_time = True
    # The source column is a long cache path that pushes rows past 170 cells.
    cfg.show_source = False
    cfg.show_copilot_credits = True
    cfg.color_enabled = cfg.color_enabled and not colors_disabled()
    cfg.progress_enabled = False
    # Keep every remaining column on one line; the Bridge view clips and pans
    # wide rows instead of wrapping them, so a table row is never split.
    cfg.terminal_width = 10000
    injected = _injected_provider_data(os.environ)
    wanted = frozenset(name.strip().lower() for name in rotation or () if name.strip())
    if injected is None and not wanted:
        return usage.dashboard_snapshot(cfg)
    provider_data = injected if injected is not None else usage._render_data_for_frame(cfg, start_ephemeral=False)[0]
    if not wanted:
        return usage.dashboard_snapshot(cfg, provider_data=provider_data)
    hide = frozenset(name for name, data in provider_data.items() if _provider_reason(data) in _NOT_INSTALLED_REASONS)
    shown, everything = usage.dashboard_views(cfg, provider_data, rotation=wanted, hide=hide)
    return {**shown, "all_text": everything["text"], "all_rows": everything["rows"],
            "rotation": sorted(wanted), "not_installed": sorted(hide)}


def rotation_providers(snapshot: Mapping[str, Any]) -> frozenset[str]:
    """Provider ids in the mission's rotation, from its projection (configured stations, then the controller's decisions)."""
    from .capacity import ALL_PROVIDERS

    names: list[str] = []
    stations = snapshot.get("stations")
    if isinstance(stations, dict):
        configured = stations.get("configured")
        if isinstance(configured, dict):
            names += [str(item) for key in ("providers", "routes") for item in (configured.get(key) or [])]
    elif isinstance(stations, list):
        names += [str(item.get("name") or "") for item in stations if isinstance(item, dict) and item.get("status") == "configured"]
    capacity = snapshot.get("capacity")
    selection = capacity.get("selection") if isinstance(capacity, dict) else None
    for decision in (selection.get("decisions") if isinstance(selection, dict) else None) or []:
        if isinstance(decision, dict):
            names += [str(decision.get("provider") or ""), str(decision.get("capacity_provider") or "")]
    return frozenset(name.strip().lower() for name in names if name.strip().lower() in ALL_PROVIDERS)


def print_usage_table(stream: Any = None) -> None:
    """Print the llm-usage table once, for `llm-bridge usage` without a terminal."""
    from . import usage
    from .bridge_theme import colors_disabled
    cfg = usage.Config()
    cfg.show_remaining_time = True
    cfg.show_source = False
    cfg.show_copilot_credits = True
    cfg.color_enabled = cfg.color_enabled and not colors_disabled()
    cfg.progress_enabled = False
    text = usage.dashboard_snapshot(cfg)["text"]
    out = sys.stdout if stream is None else stream
    out.write(text if text.endswith("\n") else text + "\n")


class EmbeddedUsage:
    """One bounded refresh in flight; failures retain explicitly stale output."""

    def __init__(self, *, collector: Callable[[], dict[str, Any]] | None = None, interval: float = 60):
        if interval < 5:
            raise ValueError('usage refresh interval must be at least 5 seconds')
        self.rotation: frozenset[str] = frozenset()
        self.collector = collector or (lambda: collect_usage(self.rotation))
        self.interval = interval
        self._lock = threading.Lock()
        self._running = False
        self._next_refresh = 0.0
        self._observed: float | None = None
        self._data: dict[str, Any] = {}
        self._error = ''

    def set_rotation(self, rotation: Iterable[str]) -> None:
        """Limit the default view to these provider ids; a change refreshes at the next snapshot."""
        wanted = frozenset(rotation)
        with self._lock:
            if wanted != self.rotation:
                self.rotation = wanted
                self._next_refresh = 0.0

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        current = time.monotonic() if now is None else now
        with self._lock:
            if not self._running and current >= self._next_refresh:
                self._running = True
                self._next_refresh = current + self.interval
                threading.Thread(target=self._refresh, args=(current,), daemon=True,
                                 name='bridge-usage').start()
            return {**self._data,
                    'status': 'refreshing' if self._running else 'unavailable' if self._error else 'ready',
                    'error': self._error,
                    'age_seconds': max(0, int(current-self._observed)) if self._observed is not None else None}

    def _refresh(self, started: float) -> None:
        try:
            data = self.collector()
            if not isinstance(data.get('text'), str) or not isinstance(data.get('rows'), list):
                raise ValueError('invalid usage dashboard')
            with self._lock:
                self._data = data
                self._observed = started
                self._error = ''
        except (Exception, SystemExit) as exc:
            with self._lock:
                self._error = type(exc).__name__
        finally:
            with self._lock:
                self._running = False


embedded_usage = EmbeddedUsage()
