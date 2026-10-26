"""Nonblocking embedded llm-usage dashboard, using its existing Python API."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable


def collect_usage() -> dict[str, Any]:
    from . import usage
    cfg = usage.Config()
    cfg.show_remaining_time = True
    cfg.show_source = True
    cfg.show_copilot_credits = True
    cfg.color_enabled = False
    cfg.progress_enabled = False
    # Keep all columns; the Bridge view wraps them to the available width.
    cfg.terminal_width = 10000
    return usage.dashboard_snapshot(cfg)


class EmbeddedUsage:
    """One bounded refresh in flight; failures retain explicitly stale output."""

    def __init__(self, *, collector: Callable[[], dict[str, Any]] = collect_usage, interval: float = 60):
        if interval < 5:
            raise ValueError('usage refresh interval must be at least 5 seconds')
        self.collector = collector
        self.interval = interval
        self._lock = threading.Lock()
        self._running = False
        self._next_refresh = 0.0
        self._observed = None
        self._data: dict[str, Any] = {}
        self._error = ''

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
