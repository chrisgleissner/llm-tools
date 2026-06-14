"""Kilo Code CLI provider adapter.

Kilo's governing constraints are not session windows:

* ``balance``  : funded credits / balance, no natural reset.
* ``budget``   : optional monthly budget pacing with a reset.
* ``byok`` / ``local`` / ``ungated`` : the CLI is usable but the host
  rate limits are not the constraint.

The reader tries three sources in order, mirroring the Codex/Claude/Copilot
pattern of preferring real CLI output and falling back to deterministic env
vars:

1. ``kilo stats`` JSON (when the ``kilo`` binary is on PATH and emits a
   parseable payload).
2. ``kilo stats`` text (best-effort key/value parse for stable fields).
3. Environment variables:

   * ``LLM_USAGE_KILO_MODE`` - ``gateway`` (default), ``budget``,
     ``byok``, ``local``, ``ungated``.
   * ``LLM_USAGE_KILO_BALANCE`` - remaining balance (number).
   * ``LLM_USAGE_KILO_CURRENCY`` - unit label (``GBP``/``USD``/``credits``).
   * ``LLM_USAGE_KILO_MIN_BALANCE`` - threshold below which Kilo is
     treated as insufficient (default ``1``).
   * ``LLM_USAGE_KILO_MONTHLY_BUDGET`` / ``LLM_USAGE_KILO_MONTHLY_SPENT``
     - budget pacing.
   * ``LLM_USAGE_KILO_MONTHLY_RESET_DAY`` - day of month the budget
     resets (default ``1``).

If nothing is known and the mode is not ungated, the snapshot is
``available=false`` with reason ``inconclusive-usage`` so callers can
poll rather than fabricate a fake reset.

The CLI's mere presence is also a hard requirement: even an ungated
provider needs a binary to launch. ``read_kilo`` reports
``reason="missing-cli"`` when the binary is not on PATH.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any

from .. import common
from ..capacity import (
    CapacityKind,
    CapacityScope,
    PROVIDER_KILO,
    ProviderSnapshot,
    SCOPE_BALANCE,
    SCOPE_BUDGET,
    SCOPE_BYOK,
    SCOPE_UNGATED,
)


KILO_MODES = ("gateway", "budget", "byok", "local", "ungated")
DEFAULT_MIN_BALANCE = 1.0
DEFAULT_RESET_DAY = 1

_UNGATED_LABEL = {
    "byok": "byok",
    "local": "local",
    "ungated": "unmetered",
}


def kilo_cli(env: dict[str, str] | None = None) -> str | None:
    """Locate the ``kilo`` binary using ``env`` (defaults to ``os.environ``).

    Accepting an env parameter keeps callers deterministic in tests: the
    host's PATH may contain an unrelated ``kilo`` install, but a test
    fixture can still isolate itself.
    """
    if env is None:
        env = os.environ
    return shutil.which("kilo", path=env.get("PATH"))


def kilo_mode(env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    raw = (env.get("LLM_USAGE_KILO_MODE") or "gateway").strip().lower()
    return raw if raw in KILO_MODES else "gateway"


def kilo_min_balance(env: dict[str, str] | None = None) -> float:
    env = env or os.environ
    raw = env.get("LLM_USAGE_KILO_MIN_BALANCE")
    if raw is None or raw == "":
        return DEFAULT_MIN_BALANCE
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_BALANCE


def kilo_currency(env: dict[str, str] | None = None) -> str | None:
    env = env or os.environ
    value = env.get("LLM_USAGE_KILO_CURRENCY")
    return value or None


def kilo_monthly_reset_epoch(env: dict[str, str] | None = None) -> int:
    """Next monthly budget reset epoch, derived from
    ``LLM_USAGE_KILO_MONTHLY_RESET_DAY`` (default 1)."""
    env = env or os.environ
    try:
        day = int(env.get("LLM_USAGE_KILO_MONTHLY_RESET_DAY", str(DEFAULT_RESET_DAY)) or str(DEFAULT_RESET_DAY))
    except ValueError:
        day = DEFAULT_RESET_DAY
    day = max(1, min(31, day))
    now = common.now_epoch(env)
    dt = datetime.fromtimestamp(now)
    this_month_reset = _epoch_for_day(dt.year, dt.month, day)
    if this_month_reset > now:
        return int(this_month_reset)
    if dt.month == 12:
        nxt_year, nxt_month = dt.year + 1, 1
    else:
        nxt_year, nxt_month = dt.year, dt.month + 1
    return int(_epoch_for_day(nxt_year, nxt_month, day))


def _epoch_for_day(year: int, month: int, day: int) -> float:
    last_day = calendar.monthrange(year, month)[1]
    target = min(day, last_day)
    return time.mktime(datetime(year, month, target).timetuple())


def _parse_balance(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    # Strip a leading currency/unit symbol so "£12.40" parses.
    text = re.sub(r"^[^\d\-\+]+", "", text)
    try:
        return float(text)
    except ValueError:
        return None


def _parse_kilo_stats_payload(payload: Any) -> dict[str, Any] | None:
    """Pull a small, stable subset of fields out of a ``kilo stats`` payload.

    We intentionally accept a narrow vocabulary so a stat dump that changes
    shape tomorrow does not silently feed garbage into the budget model. A
    payload that does not match any known shape is returned as ``None`` and
    the reader falls back to env vars.
    """
    if not isinstance(payload, dict):
        return None
    out: dict[str, Any] = {}
    balance = _first(payload, ("balance", "credits", "remaining", "remaining_credits", "available_balance"))
    if balance is not None:
        out["balance"] = _parse_balance(balance)
    currency = _first(payload, ("currency", "unit", "balance_currency", "credits_unit"))
    if currency:
        out["currency"] = str(currency)
    budget = _first(payload, ("budget", "monthly_budget", "monthlyBudget", "budget_total"))
    if budget is not None:
        out["budget"] = _parse_balance(budget)
    spent = _first(payload, ("spent", "monthly_spent", "monthlySpent", "budget_used"))
    if spent is not None:
        out["spent"] = _parse_balance(spent)
    if not out:
        return None
    return out


def _parse_kilo_stats_text(text: str) -> dict[str, Any] | None:
    """Best-effort line parser for the human-readable ``kilo stats`` output.

    The default output is a TUI-shaped layout with two blocks
    (``OVERVIEW`` and ``COST & TOKENS``); each row is
    ``│Key                  Value unit│``. We accept that shape, the
    legacy ``key: value`` / ``key = value`` line format, and a small
    set of long-form ``field: number [unit]`` lines. A trailing
    currency/unit token (e.g. ``balance: 7.50 GBP``) is folded into the
    parsed value. Anything unrecognised is ignored.
    """
    if not text:
        return None
    out: dict[str, Any] = {}
    # TUI-boxed rows: ``│Total Cost                  $7.50│``. The currency
    # symbol may prefix the number ("$7.50") or follow it ("7.50 USD");
    # we capture the raw cell (which may include a trailing │) and
    # post-process it.
    tui_patterns = (
        (re.compile(r"^\s*│?\s*Total Cost\s+(\S+(?:\s+\S+)?)\s*│?\s*$", re.I | re.M), ("cost", "currency")),
        (re.compile(r"^\s*│?\s*Avg Cost/Day\s+(\S+(?:\s+\S+)?)\s*│?\s*$", re.I | re.M), ("avg_cost_per_day", "currency")),
        (re.compile(r"^\s*│?\s*Balance\s+(\S+(?:\s+\S+)?)\s*│?\s*$", re.I | re.M), ("balance", "currency")),
        (re.compile(r"^\s*│?\s*Credits\s+(\S+(?:\s+\S+)?)\s*│?\s*$", re.I | re.M), ("balance", "currency")),
        (re.compile(r"^\s*│?\s*Monthly Budget\s+(\S+(?:\s+\S+)?)\s*│?\s*$", re.I | re.M), ("budget", "currency")),
        (re.compile(r"^\s*│?\s*Monthly Spent\s+(\S+(?:\s+\S+)?)\s*│?\s*$", re.I | re.M), ("spent", "currency")),
    )
    for regex, keys in tui_patterns:
        match = regex.search(text)
        if not match:
            continue
        value_key, extra_key = keys
        if value_key in out:
            continue
        raw_cell = match.group(1).strip().rstrip("│").rstrip()
        m = re.match(r"^([\$€£]?)([0-9][0-9.,]*)\s*([A-Za-z€£$]+)?$", raw_cell)
        if m:
            prefix, value, suffix = m.group(1), m.group(2), m.group(3)
            parsed = _parse_balance(value)
            if parsed is not None:
                out[value_key] = parsed
            currency = prefix or suffix
            if extra_key and currency and extra_key not in out:
                out[extra_key] = currency
        else:
            parsed = _parse_balance(raw_cell)
            if parsed is not None:
                out[value_key] = parsed

    patterns = (
        (re.compile(r"balance[:=]\s*([0-9]+(?:\.[0-9]+)?)(?:\s+([A-Za-z€£$]+))?", re.I), ("balance", "currency")),
        (re.compile(r"\bcredits?[:=]\s*([0-9]+(?:\.[0-9]+)?)(?:\s+([A-Za-z€£$]+))?", re.I), ("balance", "currency")),
        (re.compile(r"currency[:=]\s*([^\s,;]+)", re.I), ("currency", None)),
        (re.compile(r"\bunit[:=]\s*([^\s,;]+)", re.I), ("currency", None)),
        (re.compile(r"budget[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.I), ("budget", None)),
        (re.compile(r"monthly[_\s-]?budget[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.I), ("budget", None)),
        (re.compile(r"spent[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.I), ("spent", None)),
        (re.compile(r"monthly[_\s-]?spent[:=]\s*([0-9]+(?:\.[0-9]+)?)", re.I), ("spent", None)),
    )
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for regex, keys in patterns:
            match = regex.search(line)
            if not match:
                continue
            value = match.group(1).strip().rstrip(",;")
            value_key, extra_key = keys
            if value_key in ("balance", "budget", "spent"):
                if value_key not in out:
                    parsed = _parse_balance(value)
                    if parsed is not None:
                        out[value_key] = parsed
            else:
                if value_key not in out:
                    out[value_key] = value
            if extra_key and match.group(2) and extra_key not in out:
                out[extra_key] = match.group(2).strip()
            break
    return out or None


def _first(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def _run_kilo_stats(env: dict[str, str]) -> dict[str, Any] | None:
    cli = kilo_cli(env)
    if not cli:
        return None
    try:
        proc = subprocess.run(
            [cli, "stats"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=int(env.get("LLM_USAGE_KILO_TIMEOUT", "10") or "10"),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    text = proc.stdout.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _parse_kilo_stats_text(text)
    return _parse_kilo_stats_payload(payload)


def _balance_from_env(env: dict[str, str]) -> float | None:
    return _parse_balance(env.get("LLM_USAGE_KILO_BALANCE"))


def _budget_from_env(env: dict[str, str]) -> tuple[float | None, float | None]:
    budget = _parse_balance(env.get("LLM_USAGE_KILO_MONTHLY_BUDGET"))
    spent = _parse_balance(env.get("LLM_USAGE_KILO_MONTHLY_SPENT"))
    return budget, spent


def _scopes_for_mode(
    mode: str,
    *,
    balance: float | None,
    currency: str | None,
    budget_total: float | None,
    budget_spent: float | None,
    reset_epoch: int,
    env: dict[str, str],
) -> list[CapacityScope]:
    scopes: list[CapacityScope] = []
    source_parts: list[str] = []
    stats = _run_kilo_stats(env)
    if stats is not None:
        source_parts.append("kilo stats")
        if balance is None and stats.get("balance") is not None:
            balance = stats["balance"]
        if currency is None and stats.get("currency"):
            currency = stats["currency"]
        if budget_total is None and stats.get("budget") is not None:
            budget_total = stats["budget"]
        if budget_spent is None and stats.get("spent") is not None:
            budget_spent = stats["spent"]

    # Always add the explicit env-vars source if anything was set, to make
    # tests easy to read.
    if any(env.get(k) for k in ("LLM_USAGE_KILO_BALANCE", "LLM_USAGE_KILO_CURRENCY", "LLM_USAGE_KILO_MONTHLY_BUDGET", "LLM_USAGE_KILO_MONTHLY_SPENT")):
        source_parts.append("env")
    if not source_parts:
        source_parts.append("kilo cli")
    source = " + ".join(source_parts)

    if mode in ("byok", "local", "ungated"):
        label = _UNGATED_LABEL[mode]
        scopes.append(
            CapacityScope(
                name="ungated" if mode == "ungated" else mode,
                kind=CapacityKind.UNGATED,
                ready=True,
                reason=mode,
                label=label,
                source=source,
                extras={"mode": mode, "cli": kilo_cli(env) or ""},
            )
        )
        return scopes

    if budget_total is not None and budget_total > 0:
        remaining_amount = None
        remaining_percent = None
        if budget_spent is not None:
            remaining_amount = max(0.0, budget_total - budget_spent)
            remaining_percent = max(0.0, min(100.0, remaining_amount / budget_total * 100.0))
        scopes.append(
            CapacityScope(
                name=SCOPE_BUDGET,
                kind=CapacityKind.BUDGET,
                remaining_percent=remaining_percent,
                remaining_amount=remaining_amount,
                total_amount=budget_total,
                currency=currency,
                reset_epoch=reset_epoch,
                resets_at=reset_epoch,
                source=source,
            )
        )

    if balance is not None:
        scopes.append(
            CapacityScope(
                name=SCOPE_BALANCE,
                kind=CapacityKind.BALANCE,
                remaining_amount=balance,
                currency=currency,
                source=source,
            )
        )

    # If ``kilo stats`` reported a cost but we have no configured
    # balance or budget, surface the cost as a spent row so the user
    # sees real numbers in the table instead of ``inconclusive-usage``.
    cost = stats.get("cost") if stats else None
    if (
        cost is not None
        and balance is None
        and not any(s.kind == CapacityKind.BUDGET for s in scopes)
    ):
        cost_currency = currency or (stats.get("currency") if stats else None)
        scopes.append(
            CapacityScope(
                name=SCOPE_BALANCE,
                kind=CapacityKind.BALANCE,
                remaining_amount=float(cost),
                currency=cost_currency,
                source=source,
                extras={"spent": True},
            )
        )

    if not scopes:
        scopes.append(
            CapacityScope(
                name="usage",
                kind=CapacityKind.UNKNOWN,
                ready=False,
                reason="inconclusive-usage",
                source=source,
            )
        )
    return scopes


def read_kilo(env: dict[str, str] | None = None) -> ProviderSnapshot:
    env = env or os.environ
    cli = kilo_cli(env)
    mode = kilo_mode(env)
    balance = _balance_from_env(env)
    currency = kilo_currency(env)
    budget_total, budget_spent = _budget_from_env(env)
    reset_epoch = kilo_monthly_reset_epoch(env)
    scopes = _scopes_for_mode(
        mode,
        balance=balance,
        currency=currency,
        budget_total=budget_total,
        budget_spent=budget_spent,
        reset_epoch=reset_epoch,
        env=env,
    )

    if mode in ("byok", "local", "ungated"):
        if not cli:
            return ProviderSnapshot(
                provider=PROVIDER_KILO,
                available=False,
                reason="missing-cli",
                source="kilo cli",
                scopes=scopes,
            )
        return ProviderSnapshot(
            provider=PROVIDER_KILO,
            available=True,
            source=scopes[0].source if scopes else "kilo cli",
            selected_model=None,
            scopes=scopes,
        )

    # gateway / budget: usable only if we actually have at least one
    # known data scope. When env-var data is present, the snapshot is
    # usable even without a CLI install (the CLI is only required at
    # launch time, which the scheduler checks separately).
    has_data = any(s.kind != CapacityKind.UNKNOWN for s in scopes)
    if not has_data:
        return ProviderSnapshot(
            provider=PROVIDER_KILO,
            available=False,
            reason="inconclusive-usage",
            source=scopes[0].source if scopes else "kilo cli",
            scopes=scopes,
        )
    has_env_data = bool(
        env.get("LLM_USAGE_KILO_BALANCE")
        or env.get("LLM_USAGE_KILO_MONTHLY_BUDGET")
    )
    if not cli and not has_env_data and mode != "ungated":
        return ProviderSnapshot(
            provider=PROVIDER_KILO,
            available=False,
            reason="missing-cli",
            source=scopes[0].source if scopes else "kilo cli",
            scopes=scopes,
        )
    return ProviderSnapshot(
        provider=PROVIDER_KILO,
        available=True,
        source=scopes[0].source if scopes else "kilo cli",
        selected_model=None,
        scopes=scopes,
    )


def kilo_command_argv(cfg_attached: bool, cwd: str, prompt: str) -> list[str]:
    """Build the default argv for launching Kilo.

    Attached/interactive: ``kilo run <prompt>`` (run from --cwd).
    Headless/autonomous: ``kilo run --auto <prompt>``.
    """
    base = ["kilo", "run"]
    if not cfg_attached:
        base.append("--auto")
    return [*base, prompt]


__all__ = [
    "KILO_MODES",
    "kilo_cli",
    "kilo_command_argv",
    "kilo_currency",
    "kilo_min_balance",
    "kilo_mode",
    "kilo_monthly_reset_epoch",
    "read",
    "read_kilo",
]


def read(env: dict[str, str] | None = None) -> ProviderSnapshot:
    """Consistent with the other provider modules: ``read(env)`` returns a
    :class:`ProviderSnapshot`. The actual implementation lives in
    :func:`read_kilo`; this is the public name used by
    :mod:`llm_tools.providers` callers.
    """
    return read_kilo(env)
