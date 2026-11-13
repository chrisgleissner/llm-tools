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
SPEND_PROVIDERS = ("kilo", "copilot")  # the providers whose llm-usage snapshot carries a spend figure
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


def normalize_currency(value: Any) -> str:
    """``$`` -> ``USD`` and so on; any other value upper-cased."""
    text = str(value or "").strip()
    return _CURRENCY_SYMBOLS.get(text, text.upper())
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
            out[key] = normalize_currency(value)
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
            if normalize_currency(data.get("currency")) == budget["currency"]:
                total += float(data.get("amount") or 0)
        used["spend"] = round(total, 6)
    return used


def reached(budget: dict[str, Any], used: dict[str, Any]) -> list[str]:
    return [key for key in LIMIT_KEYS if key in budget and key in used and used[key] >= budget[key]]


def check(store: Any, mission: dict[str, Any], *, now: float | None = None, only: tuple[str, ...] = LIMIT_KEYS) -> dict[str, Any] | None:
    """``{"limits": [...], "budget", "used"}`` when a limit is reached, else None. ``only`` restricts which limits count."""
    budget = {key: value for key, value in budget_of(mission.get("config")).items() if key in only or key == "currency"}
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


def hold_if_reached(store: Any, mission: dict[str, Any], *, now: float | None = None, only: tuple[str, ...] = LIMIT_KEYS) -> dict[str, Any] | None:
    """Hold a mission that reached a limit and record one ``budget-exhausted`` event. Returns the check result.

    The hold never ends a provider run: it pauses the mission, so the current run finishes and the next one does not start.
    """
    hit = check(store, mission, now=now, only=only)
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


def _reading_from_snapshot(provider: str, snapshot: dict[str, Any]) -> tuple[float, str] | None:
    """``(amount, normalised currency)`` of a provider's month-to-date spend figure in its llm-usage snapshot, else None."""
    if provider == "copilot":
        addon = snapshot.get("add_on")
        if isinstance(addon, dict) and isinstance(addon.get("spent"), (int, float)) and not isinstance(addon.get("spent"), bool):
            return float(addon["spent"]), normalize_currency(addon.get("currency") or "$")
        return None
    for scope in snapshot.get("scopes") or []:
        if not isinstance(scope, dict):
            continue
        raw_extras = scope.get("extras")
        extras: dict[str, Any] = raw_extras if isinstance(raw_extras, dict) else {}
        if (scope.get("spent") or extras.get("spent")) and isinstance(scope.get("remaining_amount"), (int, float)):
            return float(scope["remaining_amount"]), normalize_currency(scope.get("currency") or "$")
    return None


def rotation_providers(selection: dict[str, Any]) -> list[str]:
    """Spend-capable providers named by a selection's decisions."""
    names: list[str] = []
    for decision in selection.get("decisions") or []:
        if isinstance(decision, dict):
            for key in ("provider", "capacity_provider"):
                name = str(decision.get(key) or "")
                if name in SPEND_PROVIDERS and name not in names:
                    names.append(name)
    return names


def spend_readings(providers: list[str], env: Any = None) -> dict[str, tuple[float, str]]:
    import os

    from . import common

    found: dict[str, tuple[float, str]] = {}
    for name in providers:
        try:
            reading = _reading_from_snapshot(name, common.usage_snapshot_for_provider(name, dict(os.environ if env is None else env)))
        except Exception:  # noqa: BLE001 - one unreadable provider must not hide the others
            continue
        if reading is not None:
            found[name] = reading
    return found


def sample_spend(store: Any, mission: dict[str, Any], selection: dict[str, Any], *, env: Any = None) -> list[dict[str, Any]]:
    """Record spend since the mission's baseline as ``provider-spend`` events; returns the events' data.

    The first reading of each provider is the baseline (a provider's figure is month to date, so earlier spend is not the
    mission's). Later readings emit the increase in the budget currency. The emitted total never decreases; a reading below
    the previous one is a new billing month, so the baseline restarts at 0 and the total emitted so far carries over.
    """
    config = mission.get("config") or {}
    budget = budget_of(config)
    if "spend" not in budget:
        return []
    mission_id = str(mission["id"])
    state: dict[str, dict[str, float]] = {name: dict(value) for name, value in (config.get("spend_state") or {}).items() if isinstance(value, dict)}
    emitted: list[dict[str, Any]] = []
    changed = False
    for name, (amount, currency) in spend_readings(rotation_providers(selection), env).items():
        if currency != budget["currency"]:
            continue
        item = state.get(name)
        if item is None:
            state[name] = {"baseline": amount, "last": amount, "offset": 0.0, "emitted": 0.0}
            changed = True
            continue
        if amount < item["last"]:  # month rollover: what was spent since is the new reading itself
            item["offset"], item["baseline"] = item["emitted"], 0.0
        total = item["offset"] + max(0.0, amount - item["baseline"])
        delta = round(max(0.0, total - item["emitted"]), 6)
        if delta > 0:
            store.record_runtime_event(mission_id, "provider-spend", {"amount": delta, "currency": currency, "provider": name})
            emitted.append({"provider": name, "amount": delta, "currency": currency})
            item["emitted"] = round(item["emitted"] + delta, 6)
        item["last"] = amount
        changed = True
    if changed:
        store.set_mission_config_values(mission_id, {"spend_state": state}, event_kind="spend-baseline")
    return emitted


def spend_capable(conf: dict[str, Any], providers: Any, routes: Any, currency: str, env: Any = None) -> tuple[bool, str]:
    """Whether some rotation entry can report spend in ``currency``; the message names the providers that can report spend."""
    import os

    from .bridge_rotation import entries

    environ = os.environ if env is None else env
    launch = [cli for _name, cli in entries(conf, providers, routes) if cli in SPEND_PROVIDERS]
    kilo_currency = normalize_currency(environ.get("LLM_USAGE_KILO_CURRENCY") or "$")
    capable = [cli for cli in launch if currency == ("USD" if cli == "copilot" else kilo_currency)]
    if capable:
        return True, ""
    return False, ("a spend limit needs a rotation provider that reports spend in " + currency + ": Copilot (add-on spend, USD) or Kilo (month-to-date cost, " + kilo_currency + ")"
                   + ("; this rotation has " + ", ".join(sorted(set(launch))) + " but not in that currency" if launch else "; this rotation has none of them"))
