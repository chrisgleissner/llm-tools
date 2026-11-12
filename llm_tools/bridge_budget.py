"""Mission budgets (ORCH-005).

A mission may carry limits on provider runs, wall-clock time, provider changes and spend. They are saved
in the mission config under ``budget`` as ``{"runs": N, "wall_seconds": N, "provider_changes": N,
"spend": X, "currency": "USD"}``; a missing key is no limit.

* runs: provider operations started for the mission.
* wall clock: seconds from the mission's first provider operation to now.
* provider changes: how often consecutive operations used a different provider or route.
* spend: the sum of ``provider-spend`` events in the budget's currency. A provider that reports no cost
  adds nothing, so a spend limit is never enforced on a guess; a spend limit needs a currency.

A limit is reached when usage is at or above it. The controller checks before it starts a run; a mission
that has reached a limit is held (``PAUSED``, ``hold_reason = budget-exhausted``) until the operator raises
the limit with ``llm-bridge mission set``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

HOLD_REASON = "budget-exhausted"
LIMIT_KEYS = ("runs", "wall_seconds", "provider_changes", "spend")
LABELS = {"runs": "provider runs", "wall_seconds": "wall-clock time", "provider_changes": "provider changes", "spend": "spend"}


def validate_budget(budget: Any) -> dict[str, Any]:
    """Normalise a budget dictionary; raise ``ValueError`` with an operator-facing message."""
    if not isinstance(budget, dict):
        raise ValueError("budget must be an object")
    out: dict[str, Any] = {}
    for key, value in budget.items():
        if key == "currency":
            if not isinstance(value, str) or not value.strip() or len(value) > 8:
                raise ValueError("budget currency must be a short code such as USD")
            out[key] = value.strip().upper()
        elif key in {"runs", "wall_seconds", "provider_changes"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"budget {key.replace('_', ' ')} must be a whole number of at least 1")
            out[key] = value
        elif key == "spend":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError("budget spend must be a number above 0")
            out[key] = float(value)
        else:
            raise ValueError(f"unknown budget limit: {key}")
    if "spend" in out and "currency" not in out:
        raise ValueError("a spend limit needs a currency (for example --budget-currency USD): Bridge counts spend only where a provider reports one")
    if "spend" not in out:
        out.pop("currency", None)
    return out


def budget_of(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = config.get("budget") if isinstance(config, dict) else None
    try:
        return validate_budget(raw) if raw else {}
    except ValueError:
        return {}


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp() if text else 0.0


def usage(store: Any, mission_id: str, budget: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Current usage for each limit that is set."""
    used: dict[str, Any] = {}
    rows = store.db.execute("SELECT station,created_at FROM operations WHERE mission_id=? ORDER BY rowid", (mission_id,)).fetchall()
    if "runs" in budget:
        used["runs"] = len(rows)
    if "provider_changes" in budget:
        used["provider_changes"] = sum(1 for before, after in zip(rows, rows[1:]) if before["station"] != after["station"])
    if "wall_seconds" in budget:
        used["wall_seconds"] = int(max(0.0, (time.time() if now is None else now) - _epoch(rows[0]["created_at"]))) if rows else 0
    if "spend" in budget:
        total = 0.0
        for row in store.db.execute("SELECT data_json FROM events WHERE mission_id=? AND kind='provider-spend'", (mission_id,)):
            data = json.loads(row["data_json"])
            if str(data.get("currency", "")).upper() == budget["currency"]:
                total += float(data.get("amount") or 0)
        used["spend"] = round(total, 6)
    return used


def reached(budget: dict[str, Any], used: dict[str, Any]) -> list[str]:
    return [key for key in LIMIT_KEYS if key in budget and key in used and used[key] >= budget[key]]


def check(store: Any, mission: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
    """``{"limits": [...], "budget", "used"}`` when a limit is reached, else None."""
    budget = budget_of(mission.get("config"))
    if not budget:
        return None
    used = usage(store, str(mission["id"]), budget, now=now)
    hit = reached(budget, used)
    return {"limits": hit, "budget": budget, "used": used} if hit else None


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:g} min" if seconds < 7200 else f"{seconds / 3600:g} h"


def describe(key: str, budget: dict[str, Any], used: dict[str, Any] | None = None) -> str:
    """One limit as text, with the usage when known: ``provider runs 3 of 3``."""
    limit = budget[key]
    have = used.get(key) if used else None
    if key == "wall_seconds":
        return f"{LABELS[key]} {'' if have is None else _minutes(have) + ' of '}{_minutes(limit)}"
    if key == "spend":
        return f"{LABELS[key]} {'' if have is None else f'{have:g} of '}{limit:g} {budget.get('currency', '')}".rstrip()
    return f"{LABELS[key]} {'' if have is None else f'{have} of '}{limit}"


def summary(budget: dict[str, Any], used: dict[str, Any] | None = None) -> str:
    return " · ".join(describe(key, budget, used) for key in LIMIT_KEYS if key in budget) or "no limits"


def hold_if_reached(store: Any, mission: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
    """Hold a mission that reached a limit and record one ``budget-exhausted`` event. Returns the check result."""
    hit = check(store, mission, now=now)
    if hit is None:
        return None
    from .bridge_publish import hold_mission

    mission_id = str(mission["id"])
    detail = "limit reached: " + "; ".join(describe(key, hit["budget"], hit["used"]) for key in hit["limits"])
    store.record_runtime_event(mission_id, "budget-exhausted", {
        "limits": hit["limits"], "budget": hit["budget"], "used": hit["used"], "summary": detail,
        "command": f"llm-bridge mission set --mission {mission_id[:8]} --budget-runs N"})
    hold_mission(store, mission_id, HOLD_REASON, detail)
    return hit


def release_if_raised(store: Any, mission_id: str) -> bool:
    """After a settings change: when the mission was held for its budget and no limit is reached, release the hold and resume."""
    mission = store.get_mission(mission_id)
    config = mission.get("config") or {}
    if config.get("hold_reason") != HOLD_REASON or check(store, mission) is not None:
        return False
    store.set_mission_config_values(mission_id, {"hold_reason": "", "hold_detail": ""}, event_kind="mission-hold-released")
    if str(mission["status"]) == "PAUSED":
        store.set_mission_status(mission_id, "RUNNING")
    return True
