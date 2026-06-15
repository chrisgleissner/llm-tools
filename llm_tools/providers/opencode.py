"""OpenCode CLI provider adapter.

OpenCode (https://opencode.ai) is a TUI / CLI for the OpenCode AI
agent. Like Kilo, its governing constraints are not session windows:

* ``balance``  : funded credit balance (parsed from ``opencode stats``
  TUI output, or supplied via ``LLM_USAGE_OPENCODE_BALANCE``).
* ``budget``   : optional monthly budget pacing
  (``LLM_USAGE_OPENCODE_MONTHLY_BUDGET`` /
  ``LLM_USAGE_OPENCODE_MONTHLY_SPENT``).
* ``byok`` / ``local`` / ``ungated`` : BYOK / local model / unmetered
  modes where OpenCode itself is the client and the host rate limits
  are not the constraint.

The reader tries three sources in order, mirroring the Kilo / Codex /
Claude pattern:

1. ``opencode stats`` JSON (when the ``opencode`` binary is on PATH and
   emits a parseable payload).
2. ``opencode stats`` text (best-effort parse for the TUI-shaped
   ``OVERVIEW`` / ``COST & TOKENS`` blocks).
3. Environment variables for deterministic configuration and tests.

The CLI's mere presence is also a hard requirement: even an ungated
provider needs a binary to launch. ``read_opencode`` reports
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
    PROVIDER_OPENCODE,
    ProviderSnapshot,
    SCOPE_BALANCE,
    SCOPE_BUDGET,
)


OPENCODE_MODES = ("gateway", "budget", "byok", "local", "ungated")
DEFAULT_MIN_BALANCE = 1.0
DEFAULT_RESET_DAY = 1

_UNGATED_LABEL = {
    "byok": "byok",
    "local": "local",
    "ungated": "unmetered",
}


def opencode_cli(env: dict[str, str] | None = None) -> str | None:
    """Locate the ``opencode`` binary using ``env`` (defaults to
    ``os.environ``). Accepting an env parameter keeps callers
    deterministic in tests: the host's PATH may contain an unrelated
    ``opencode`` install, but a test fixture can still isolate itself.
    """
    if env is None:
        env = os.environ
    return shutil.which("opencode", path=env.get("PATH"))


def opencode_mode(env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    raw = (env.get("LLM_USAGE_OPENCODE_MODE") or "gateway").strip().lower()
    return raw if raw in OPENCODE_MODES else "gateway"


def opencode_min_balance(env: dict[str, str] | None = None) -> float:
    env = env or os.environ
    raw = env.get("LLM_USAGE_OPENCODE_MIN_BALANCE")
    if raw is None or raw == "":
        return DEFAULT_MIN_BALANCE
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_BALANCE


def opencode_currency(env: dict[str, str] | None = None) -> str | None:
    env = env or os.environ
    value = env.get("LLM_USAGE_OPENCODE_CURRENCY")
    return value or None


def opencode_monthly_reset_epoch(env: dict[str, str] | None = None) -> int:
    """Next monthly budget reset epoch, derived from
    ``LLM_USAGE_OPENCODE_MONTHLY_RESET_DAY`` (default 1)."""
    env = env or os.environ
    try:
        day = int(env.get("LLM_USAGE_OPENCODE_MONTHLY_RESET_DAY", str(DEFAULT_RESET_DAY)) or str(DEFAULT_RESET_DAY))
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
    text = re.sub(r",([0-9]{3})(?=[^0-9]|$)", r"\1", text)
    try:
        return float(text)
    except ValueError:
        return None


def _parse_opencode_stats_payload(payload: Any) -> dict[str, Any] | None:
    """Pull a small, stable subset of fields out of an ``opencode stats``
    JSON payload. We intentionally accept a narrow vocabulary so a stats
    dump that changes shape tomorrow does not silently feed garbage into
    the budget model. A payload that does not match any known shape is
    returned as ``None``.
    """
    if not isinstance(payload, dict):
        return None
    out: dict[str, Any] = {}
    cost = _first(payload, ("cost", "total_cost", "totalCost", "spend", "spent"))
    if cost is not None:
        out["cost"] = _parse_balance(cost)
    currency = _first(payload, ("currency", "unit", "cost_currency"))
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


def _parse_opencode_stats_text(text: str) -> dict[str, Any] | None:
    """Best-effort line parser for the human-readable ``opencode stats``
    output. The default output is a TUI-shaped layout with two blocks
    (``OVERVIEW`` and ``COST & TOKENS``); each line is
    ``key value unit``. We pick out the small set of fields we care
    about and ignore anything else.
    """
    if not text:
        return None
    out: dict[str, Any] = {}
    patterns = (
        # Allow leading TUI box-drawing characters (│) that wrap each
        # line in the default ``opencode stats`` output, e.g.
        # "│Total Cost                  $7.50│". The currency symbol may
        # prefix the number ("$7.50") or follow it ("7.50 USD").
        # We capture the raw cell (which may include a trailing │)
        # and post-process it in :func:`_parse_value_cell`.
        (re.compile(r"^\s*│?\s*Total Cost\s+(\S+(?:\s+\S+)?)\s*│?\s*$", re.I | re.M), ("cost", "currency")),
        (re.compile(r"^\s*│?\s*Avg Cost/Day\s+(\S+(?:\s+\S+)?)\s*│?\s*$", re.I | re.M), ("avg_cost_per_day", "currency")),
        (re.compile(r"^\s*│?\s*Sessions\s+(\S+?)\s*│?\s*$", re.I | re.M), ("sessions", None)),
        (re.compile(r"^\s*│?\s*Days\s+(\S+?)\s*│?\s*$", re.I | re.M), ("days", None)),
        (re.compile(r"^\s*│?\s*Input\s+(\S+?)\s*│?\s*$", re.I | re.M), ("input_tokens", None)),
        (re.compile(r"^\s*│?\s*Output\s+(\S+?)\s*│?\s*$", re.I | re.M), ("output_tokens", None)),
        (re.compile(r"^\s*│?\s*Cache Read\s+(\S+?)\s*│?\s*$", re.I | re.M), ("cache_read_tokens", None)),
        (re.compile(r"^\s*│?\s*Cache Write\s+(\S+?)\s*│?\s*$", re.I | re.M), ("cache_write_tokens", None)),
    )
    for regex, keys in patterns:
        match = regex.search(text)
        if not match:
            continue
        value_key, extra_key = keys
        raw_cell = match.group(1).strip()
        # Strip any trailing TUI box-drawing char (│).
        raw_cell = raw_cell.rstrip("│").rstrip()
        # Try to split a trailing currency/unit token off the value.
        # e.g. "7.50 USD" → ("7.50", "USD"); "$7.50" → ("7.50", "$");
        # "7.50" → ("7.50", None).
        m = re.match(r"^([\$€£]?)([0-9][0-9.,]*)\s*([A-Za-z€£$]+)?$", raw_cell)
        if m:
            prefix, value, suffix = m.group(1), m.group(2), m.group(3)
            parsed = _parse_balance(value)
            if parsed is not None and value_key not in out:
                out[value_key] = parsed
            currency = prefix or suffix
            if extra_key and currency and extra_key not in out:
                out[extra_key] = currency
        else:
            parsed = _parse_balance(raw_cell)
            if parsed is not None and value_key not in out:
                out[value_key] = parsed
    # Tolerant line-based parser as a fallback for future renames.
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for pattern, (value_key, extra_key) in (
            (r"balance[:=]\s*([0-9]+(?:\.[0-9]+)?)(?:\s+([A-Za-z€£$]+))?", ("balance", "currency")),
            (r"currency[:=]\s*([^\s,;]+)", ("currency", None)),
            (r"budget[:=]\s*([0-9]+(?:\.[0-9]+)?)", ("budget", None)),
            (r"monthly[_\s-]?budget[:=]\s*([0-9]+(?:\.[0-9]+)?)", ("budget", None)),
            (r"spent[:=]\s*([0-9]+(?:\.[0-9]+)?)", ("spent", None)),
            (r"monthly[_\s-]?spent[:=]\s*([0-9]+(?:\.[0-9]+)?)", ("spent", None)),
        ):
            m = re.search(pattern, line, re.I)
            if not m:
                continue
            v = m.group(1).strip()
            parsed = _parse_balance(v)
            if parsed is not None and value_key not in out:
                out[value_key] = parsed
            if extra_key and m.group(2):
                out[extra_key] = m.group(2).strip()
            break
    return out or None


def _first(obj: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def _run_opencode_stats(env: dict[str, str]) -> dict[str, Any] | None:
    cli = opencode_cli(env)
    if not cli:
        return None
    try:
        proc = subprocess.run(
            [cli, "stats"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=int(env.get("LLM_USAGE_OPENCODE_TIMEOUT", "10") or "10"),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not proc.stdout:
        return None
    text = proc.stdout.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _parse_opencode_stats_text(text)
    return _parse_opencode_stats_payload(payload)


def _balance_from_env(env: dict[str, str]) -> float | None:
    return _parse_balance(env.get("LLM_USAGE_OPENCODE_BALANCE"))


def _budget_from_env(env: dict[str, str]) -> tuple[float | None, float | None]:
    budget = _parse_balance(env.get("LLM_USAGE_OPENCODE_MONTHLY_BUDGET"))
    spent = _parse_balance(env.get("LLM_USAGE_OPENCODE_MONTHLY_SPENT"))
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
    stats = _run_opencode_stats(env)
    if stats is not None:
        source_parts.append("opencode stats")
        if balance is None and stats.get("balance") is not None:
            balance = stats["balance"]
        if currency is None and stats.get("currency"):
            currency = stats["currency"]
        if budget_total is None and stats.get("budget") is not None:
            budget_total = stats["budget"]
        if budget_spent is None and stats.get("spent") is not None:
            budget_spent = stats["spent"]
    if any(
        env.get(k)
        for k in (
            "LLM_USAGE_OPENCODE_BALANCE",
            "LLM_USAGE_OPENCODE_CURRENCY",
            "LLM_USAGE_OPENCODE_MONTHLY_BUDGET",
            "LLM_USAGE_OPENCODE_MONTHLY_SPENT",
        )
    ):
        source_parts.append("env")
    if not source_parts:
        source_parts.append("opencode cli")
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
                extras={"mode": mode, "cli": opencode_cli(env) or ""},
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

    # If ``opencode stats`` reported a cost but we have no configured
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


def read_opencode(env: dict[str, str] | None = None) -> ProviderSnapshot:
    env = env or os.environ
    cli = opencode_cli(env)
    mode = opencode_mode(env)
    balance = _balance_from_env(env)
    currency = opencode_currency(env)
    budget_total, budget_spent = _budget_from_env(env)
    reset_epoch = opencode_monthly_reset_epoch(env)
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
                provider=PROVIDER_OPENCODE,
                available=False,
                reason="missing-cli",
                source="opencode cli",
                scopes=scopes,
            )
        return ProviderSnapshot(
            provider=PROVIDER_OPENCODE,
            available=True,
            source=scopes[0].source if scopes else "opencode cli",
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
            provider=PROVIDER_OPENCODE,
            available=False,
            reason="inconclusive-usage",
            source=scopes[0].source if scopes else "opencode cli",
            scopes=scopes,
        )
    has_env_data = bool(
        env.get("LLM_USAGE_OPENCODE_BALANCE")
        or env.get("LLM_USAGE_OPENCODE_MONTHLY_BUDGET")
    )
    if not cli and not has_env_data and mode != "ungated":
        return ProviderSnapshot(
            provider=PROVIDER_OPENCODE,
            available=False,
            reason="missing-cli",
            source=scopes[0].source if scopes else "opencode cli",
            scopes=scopes,
        )
    return ProviderSnapshot(
        provider=PROVIDER_OPENCODE,
        available=True,
        source=scopes[0].source if scopes else "opencode cli",
        selected_model=None,
        scopes=scopes,
    )


def opencode_command_argv(cfg_attached: bool, cwd: str, prompt: str) -> list[str]:
    """Build the default argv for launching OpenCode.

    Attached/interactive: ``opencode`` picks up the cwd from the launching
    process (the root command has no ``--dir`` flag), so we set it via the
    subprocess cwd.
    Headless/autonomous: ``opencode run --dir <cwd> <prompt>`` so the agent
    works in the configured directory.

    We deliberately do not inject any permission-bypassing flag. Whether a
    headless run may act without prompting is left entirely to the user's own
    OpenCode config (its ``permission`` settings); the framework never elevates
    privileges on the user's behalf.
    """
    if cfg_attached:
        # Interactive TUI mode: opencode picks up the cwd from the
        # process, but we set it explicitly via the subprocess cwd.
        return ["opencode"]
    return ["opencode", "run", "--dir", cwd, prompt]


__all__ = [
    "OPENCODE_MODES",
    "opencode_cli",
    "opencode_command_argv",
    "opencode_currency",
    "opencode_min_balance",
    "opencode_mode",
    "opencode_monthly_reset_epoch",
    "read",
    "read_opencode",
]


def read(env: dict[str, str] | None = None) -> ProviderSnapshot:
    """Consistent with the other provider modules: ``read(env)`` returns
    a :class:`ProviderSnapshot`. The actual implementation lives in
    :func:`read_opencode`; this is the public name used by
    :mod:`llm_tools.providers` callers.
    """
    return read_opencode(env)
