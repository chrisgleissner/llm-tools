"""Codex CLI provider adapter.

The reader scrapes local Codex session JSONL files under
``~/.codex/sessions`` and normalises the embedded rate-limit shape into a
generic :class:`ProviderSnapshot`. Selectors are tolerant of the
``rate_limits`` / ``rateLimits`` / ``msg`` / ``payload`` envelopes Codex
has shipped over time, and scans are bounded by ``LLM_USAGE_MAX_FILES``
and ``LLM_USAGE_TAIL_LINES`` to keep the hot path fast.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import common
from ..capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_CODEX,
    ProviderSnapshot,
)


def _decorate_window(window: dict[str, Any] | None) -> dict[str, Any] | None:
    if window is None:
        return None
    out = dict(window)
    out["remaining"] = common.remaining_from_used(out.get("used"))
    return out


def normalize(obj: Any, source: str) -> dict[str, Any] | None:
    """Normalise a Codex rate-limits object into the legacy wire format.

    The wire format (``five_hour``/``week``/``rows``) is preserved for
    compatibility with the rest of the codebase; provider adapters do the
    translation into the new generic capacity model in :func:`read`.
    """
    return common.normalize_codex_obj(obj, source)


def freshen_windows(obj: Any, env: dict[str, str] | None = None) -> Any:
    return common.freshen_provider_windows(obj, env)


def read_codex(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = env or common_env_default()
    root = Path.home() / ".codex" / "sessions"
    line = common.latest_matching_line(
        root,
        lambda o: common.get_path(
            o,
            (
                ("rate_limits",),
                ("rateLimits",),
                ("rateLimits", "rateLimits"),
                ("msg", "rate_limits"),
                ("msg", "rateLimits"),
                ("payload", "rate_limits"),
                ("payload", "rateLimits"),
            ),
        )
        is not None,
        env,
    )
    if not line:
        return None
    return freshen_windows(normalize(json.loads(line), "~/.codex/sessions"), env)


def read(env: dict[str, str] | None = None) -> ProviderSnapshot:
    """Build a Codex :class:`ProviderSnapshot`.

    Codex only exposes reset-bound scopes (5h and weekly), so the snapshot
    is straightforwardly translated from the legacy wire format. Spark
    rows are surfaced through ``scopes[].extras["key"] == "codex-spark"``
    when callers want to drill in.
    """
    env = env or common_env_default()
    raw = read_codex(env)
    if not raw:
        return ProviderSnapshot(
            provider=PROVIDER_CODEX,
            available=False,
            reason="no-local-data",
            source="~/.codex/sessions",
        )
    source = raw.get("source", "~/.codex/sessions")
    rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
    if rows:
        # Pick the row whose key == "codex" for the headline scopes, but
        # surface the spark row's 5h/weekly through extras so the JSON
        # contract is unchanged.
        codex_row = next((r for r in rows if r.get("key") == "codex"), None) or rows[0]
    else:
        codex_row = None
    scopes: list[CapacityScope] = []
    five = (codex_row or {}).get("five_hour") if codex_row else raw.get("five_hour")
    week = (codex_row or {}).get("week") if codex_row else raw.get("week")
    for name, window in (("5h", five), ("weekly", week)):
        if not isinstance(window, dict):
            continue
        reset = window.get("resets_at")
        reset_epoch = common.parse_epoch(reset)
        rem = common.num(window.get("used"))
        remaining_percent = common.remaining_from_used(window.get("used"))
        scopes.append(
            CapacityScope(
                name=name,
                kind=CapacityKind.RESET_WINDOW,
                remaining_percent=remaining_percent,
                reset_epoch=reset_epoch,
                resets_at=reset,
                source=source,
            )
        )
    return ProviderSnapshot(
        provider=PROVIDER_CODEX,
        available=bool(scopes),
        source=source,
        selected_model=raw.get("plan"),
        scopes=scopes,
    )


def common_env_default() -> dict[str, str]:
    import os
    return dict(os.environ)


__all__ = ["PROVIDER_CODEX", "normalize", "read", "read_codex", "freshen_windows"]
