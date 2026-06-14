"""Claude Code provider adapter.

The Claude reader preserves the legacy fallback order: OAuth API →
cached API → statusline cache → local project JSONL. Each source is
normalised into the same wire format so the rest of the codebase does
not need to know which one won.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import common
from ..capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_CLAUDE,
    ProviderSnapshot,
)


def normalize(obj: Any, source: str) -> dict[str, Any] | None:
    return common.normalize_claude_obj(obj, source)


def freshen_windows(obj: Any, env: dict[str, str] | None = None) -> Any:
    return common.freshen_provider_windows(obj, env)


def read_claude_api(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    from .. import common

    return common.read_claude_api(env)


def read_claude(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    from .. import common

    return common.read_claude(env)


def read(env: dict[str, str] | None = None) -> ProviderSnapshot:
    raw = read_claude(env)
    if not raw:
        return ProviderSnapshot(
            provider=PROVIDER_CLAUDE,
            available=False,
            reason="no-local-data",
            source="~/.claude/projects",
        )
    source = raw.get("source", "claude api")
    scopes: list[CapacityScope] = []
    for name, key in (("5h", "five_hour"), ("weekly", "week")):
        window = raw.get(key)
        if not isinstance(window, dict):
            continue
        reset = window.get("resets_at")
        reset_epoch = common.parse_epoch(reset)
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
        provider=PROVIDER_CLAUDE,
        available=bool(scopes),
        source=source,
        scopes=scopes,
    )


__all__ = ["PROVIDER_CLAUDE", "normalize", "read", "read_claude", "read_claude_api", "freshen_windows"]
