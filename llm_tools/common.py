from __future__ import annotations

import calendar
import errno
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AUTONOMY_ABORT_STATUS = 75
TRANSIENT_COPILOT_CACHE_REASONS = {"capture-error", "format-changed", "refresh-pending", "timeout"}
DEFAULT_LOCAL_SNAPSHOT_MAX_AGE_SECONDS = 60
CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_OAUTH_CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_OAUTH_USER_AGENT = "claude-code/2.1.177"
# Codex exposes live, turn-free rate limits through its app-server JSON-RPC
# protocol (`account/rateLimits/read`). This is the active-refresh path that
# keeps `llm-usage` from ever falling back to a stale on-disk session snapshot
# while the CLI is installed and authenticated.
DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS = 15


def err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def cache_root(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    base = env.get("XDG_CACHE_HOME") or str(home_dir(env) / ".cache")
    return Path(base) / "llm-tools"


def migrate_legacy_cache_dirs(env: dict[str, str] | None = None) -> None:
    env = env or os.environ
    legacy_root = Path(env.get("XDG_CACHE_HOME") or str(home_dir(env) / ".cache"))
    root = cache_root(env)
    for name in ("llm-usage", "llm-scheduler", "ralph-robin"):
        old = legacy_root / name
        new = root / name
        if old.is_dir() and not new.exists():
            try:
                root.mkdir(parents=True, exist_ok=True)
                old.rename(new)
            except OSError:
                pass


def usage_cache_dir(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "llm-usage"


def home_dir(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    return Path(env.get("HOME") or str(Path.home()))


def scheduler_log_dir(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "llm-scheduler" / "logs"


def ralph_log_dir(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "ralph-robin" / "logs"


def ralph_state_file(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "ralph-robin" / "state.json"


def soak_log_dir(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "llm-sleep-soak" / "logs"


def have_cmd(name: str) -> bool:
    return shutil.which(name) is not None


def require_cmd(name: str) -> None:
    if not have_cmd(name):
        err(f"required command not found: {name}")
        raise SystemExit(127)


def is_number(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]+(?:[.][0-9]+)?", value or ""))


def is_integer(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]+", value or ""))


def now_epoch(env: dict[str, str] | None = None) -> int:
    env = env or os.environ
    override = env.get("LLM_USAGE_NOW_EPOCH")
    if override:
        try:
            return int(float(override))
        except ValueError:
            return int(time.time())
    return int(time.time())


def parse_epoch(value: Any) -> int | None:
    if value is None or value == "" or value == "null":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value)
    if re.fullmatch(r"[0-9]+(?:[.][0-9]+)?", text):
        return int(float(text))
    normalized = re.sub(r"\.[0-9]+", "", text)
    normalized = re.sub(r"[+-]00:00$", "Z", normalized)
    try:
        if normalized.endswith("Z"):
            return int(datetime.fromisoformat(normalized[:-1] + "+00:00").timestamp())
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        pass
    if have_cmd("date"):
        proc = subprocess.run(
            ["date", "-d", text, "+%s"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return int(proc.stdout.strip())
            except ValueError:
                return None
    return None


def fmt_reset(value: Any) -> str:
    epoch = parse_epoch(value)
    if epoch is None:
        return ""
    if have_cmd("date"):
        proc = subprocess.run(
            ["date", "-d", f"@{epoch}", "+%Y-%m-%d %H:%M"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def format_local_epoch(epoch: int) -> str:
    if have_cmd("date"):
        proc = subprocess.run(
            ["date", "-d", f"@{epoch}", "+%Y-%m-%d %H:%M:%S %Z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S %Z")


def fmt_duration(seconds: Any) -> str:
    if seconds in (None, "", "-"):
        return "-"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if total <= 0:
        return "0m"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return " ".join(parts)


def time_until(value: Any, env: dict[str, str] | None = None) -> str:
    epoch = parse_epoch(value)
    if epoch is None:
        return "-"
    return fmt_duration(max(0, epoch - now_epoch(env)))


def copilot_monthly_reset_epoch(env: dict[str, str] | None = None) -> int | None:
    env = env or os.environ
    try:
        offset = int(env.get("LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS", "0"))
    except ValueError:
        offset = 0
    now = now_epoch(env)
    dt = datetime.fromtimestamp(now)
    this = datetime(dt.year, dt.month, 1)
    this_epoch = int(time.mktime(this.timetuple())) + offset * 86400
    if this_epoch > now:
        return this_epoch
    if dt.month == 12:
        nxt = datetime(dt.year + 1, 1, 1)
    else:
        nxt = datetime(dt.year, dt.month + 1, 1)
    return int(time.mktime(nxt.timetuple())) + offset * 86400


def copilot_monthly_window_days(env: dict[str, str] | None = None) -> float:
    env = env or os.environ
    try:
        offset = int(env.get("LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS", "0"))
    except ValueError:
        offset = 0
    now = now_epoch(env)
    dt = datetime.fromtimestamp(now)
    this = datetime(dt.year, dt.month, 1)
    if dt.month == 12:
        nxt = datetime(dt.year + 1, 1, 1)
    else:
        nxt = datetime(dt.year, dt.month + 1, 1)
    this_epoch = int(time.mktime(this.timetuple())) + offset * 86400
    next_epoch = int(time.mktime(nxt.timetuple())) + offset * 86400
    if this_epoch > now:
        if dt.month == 1:
            prev = datetime(dt.year - 1, 12, 1)
        else:
            prev = datetime(dt.year, dt.month - 1, 1)
        return max((this_epoch - (int(time.mktime(prev.timetuple())) + offset * 86400)) / 86400.0, 1.0)
    return max((next_epoch - this_epoch) / 86400.0, 1.0)


def num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def remaining_from_used(used: Any) -> float | None:
    n = num(used)
    if n is None:
        return None
    return min(100.0, max(0.0, 100.0 - n))


def daily_budget_percent(remaining: Any, reset: Any, env: dict[str, str] | None = None) -> float | None:
    """Bounded daily budget for this window.

    This is the share of the full allowance that can be spent today while pacing
    the window to its reset. It is capped by the remaining quota: a 5h window
    resetting soon can invite spending everything left, but never more than
    everything left.

    Equals ``remaining% / max(days_until_reset, 1)``. Returns ``None`` when
    remaining or the reset time is unknown, or the reset is not in the future.
    """
    rem = num(remaining)
    if rem is None:
        return None
    epoch = parse_epoch(reset)
    if epoch is None:
        return None
    seconds = epoch - now_epoch(env)
    if seconds <= 0:
        return None
    return rem / max(seconds / 86400.0, 1.0)


def fmt_number(value: Any) -> str:
    n = num(value)
    if n is None:
        return "-"
    if float(n).is_integer():
        return str(int(n))
    return f"{round(n, 1):.1f}".rstrip("0").rstrip(".")


def fmt_pct(value: Any) -> str:
    return fmt_number(value)


ANSI_COLOR_ROLES: dict[str, str] = {
    "brand": "1;38;5;39",
    "info": "39",
    "ok": "1;38;5;77",
    "warn": "1;38;5;110",
    "error": "1;38;5;81",
    "dim": "2;39",
    "diff_add": "38;5;76",
    "diff_remove": "38;5;109",
    "diff_hunk": "1;38;5;75",
    "command": "1;38;5;74",
    "tool": "1;38;5;80",
    "stderr": "2;38;5;117",
    "heading": "1;39",
}


UTF_SYMBOL_ROLES: dict[str, str] = {
    "brand": "◆",
    "info": "•",
    "ok": "✓",
    "warn": "!",
    "error": "✕",
    "dim": "·",
    "diff_add": "+",
    "diff_remove": "−",
    "diff_hunk": "╭",
    "command": "$",
    "tool": "◆",
    "stderr": "!",
    "heading": "◆",
}

OUTPUT_BLOCK_LABELS: dict[str, str] = {
}


def color_code(role: str, env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    key = f"LLM_TOOLS_COLOR_{role.upper()}"
    return env.get(key, ANSI_COLOR_ROLES.get(role, ANSI_COLOR_ROLES["info"]))


def ansi_wrap(text: str, role: str, env: dict[str, str] | None = None) -> str:
    return f"\033[{color_code(role, env)}m{text}\033[0m"


def symbol_for(role: str, env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    if env.get("LLM_TOOLS_NO_SYMBOLS"):
        return ""
    key = f"LLM_TOOLS_SYMBOL_{role.upper()}"
    return env.get(key, UTF_SYMBOL_ROLES.get(role, ""))


def symbol_prefix(role: str, env: dict[str, str] | None = None) -> str:
    symbol = symbol_for(role, env)
    return f"{symbol} " if symbol else ""


def block_prefix(role: str, env: dict[str, str] | None = None) -> str:
    label = OUTPUT_BLOCK_LABELS.get(role)
    if not label:
        return symbol_prefix(role, env)
    symbol = symbol_for(role, env)
    return f"{symbol} {label:<6} " if symbol else f"{label:<6} "


def read_json_text(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def get_path(obj: Any, paths: Sequence[Sequence[str]]) -> Any:
    for path in paths:
        cur = obj
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if cur is not None:
            return cur
    return None


def window_from(obj: Any, default_minutes: int, percent_keys: Sequence[str] = ("used_percent", "usedPercent")) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    used = None
    for key in percent_keys:
        if key in obj:
            used = num(obj.get(key))
            break
    reset = obj.get("resets_at", obj.get("resetsAt"))
    minutes = num(obj.get("window_minutes", obj.get("windowDurationMins"))) or default_minutes
    if used is None and reset is None:
        return None
    return {"used": used, "resets_at": reset, "window_minutes": minutes}


def normalize_codex_obj(obj: Any, source: str) -> dict[str, Any] | None:
    rl = get_path(
        obj,
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
    if not isinstance(rl, dict):
        return None

    def as_row(name: str, key: str, row_obj: Any) -> dict[str, Any] | None:
        if not isinstance(row_obj, dict):
            return None
        primary = (
            row_obj.get("primary")
            or row_obj.get("five_hour")
            or row_obj.get("fiveHour")
            or row_obj.get("primary_window")
        )
        secondary = (
            row_obj.get("secondary")
            or row_obj.get("week")
            or row_obj.get("weekly")
            or row_obj.get("seven_day")
            or row_obj.get("sevenDay")
            or row_obj.get("secondary_window")
        )
        five = window_from(primary, 300) if isinstance(primary, dict) else None
        week = window_from(secondary, 10080) if isinstance(secondary, dict) else None
        if five is None and week is None:
            return None
        return {"key": key, "name": name, "source": source, "five_hour": five, "week": week}

    rows: list[dict[str, Any]] = []
    base = as_row("Codex", "codex", rl)
    if base:
        rows.append(base)
    spark_obj = None
    for key in (
        "spark",
        "codex_spark",
        "codexSpark",
        "gpt-5.3-codex-spark",
        "GPT-5.3-Codex-Spark",
        "gpt_5_3_codex_spark",
        "gpt53-codex-spark",
    ):
        if key in rl:
            spark_obj = rl[key]
            break
    spark = as_row("GPT-5.3-Codex-Spark", "codex-spark", spark_obj)
    if spark:
        rows.append(spark)
    for key, value in rl.items():
        if isinstance(value, dict) and "spark" in key.lower():
            row = as_row("GPT-5.3-Codex-Spark", "codex-spark", value)
            if row:
                rows.append(row)
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique.setdefault(row["key"], row)
    out_rows = list(unique.values())
    codex_row = unique.get("codex")
    return {
        "provider": "codex",
        "source": source,
        "plan": rl.get("plan_type", rl.get("planType")),
        "rows": out_rows,
        "five_hour": (codex_row or {}).get("five_hour"),
        "week": (codex_row or {}).get("week"),
    }


# Anthropic per-model weekly buckets we surface as their own display rows.
# Order controls the order the model rows appear under the Claude section.
CLAUDE_MODEL_WINDOW_KEYS: tuple[tuple[str, str], ...] = (
    ("seven_day_sonnet", "Sonnet"),
    ("seven_day_opus", "Opus"),
    ("seven_day_haiku", "Haiku"),
)


def normalize_claude_obj(obj: Any, source: str) -> dict[str, Any] | None:
    rl = get_path(
        obj,
        (
            ("rate_limits",),
            ("rateLimits",),
            ("message", "rate_limits"),
            ("message", "rateLimits"),
        ),
    )
    if rl is None and isinstance(obj, dict):
        rl = {
            "five_hour": obj.get("five_hour"),
            "seven_day": obj.get("seven_day"),
            "seven_day_sonnet": obj.get("seven_day_sonnet"),
            "seven_day_opus": obj.get("seven_day_opus"),
            "seven_day_haiku": obj.get("seven_day_haiku"),
            "extra_usage": obj.get("extra_usage"),
        }
    if not isinstance(rl, dict):
        return None
    primary = rl.get("five_hour") or rl.get("fiveHour") or rl.get("primary")
    secondary = rl.get("seven_day") or rl.get("sevenDay") or rl.get("weekly") or rl.get("secondary")
    percent_keys = ("used_percentage", "usedPercent", "used_percent", "utilization")
    out: dict[str, Any] = {
        "provider": "claude",
        "source": source,
        "plan": None,
        "five_hour": window_from(primary, 300, percent_keys) if isinstance(primary, dict) else None,
        "week": window_from(secondary, 10080, percent_keys) if isinstance(secondary, dict) else None,
    }
    # Per-model weekly limits (Anthropic exposes Sonnet/Opus/Haiku as their own
    # `seven_day_<model>` buckets alongside the aggregate `seven_day`). These are
    # display-only; the scheduler still gates on the aggregate window.
    model_weeks: list[dict[str, Any]] = []
    for src_key, label in CLAUDE_MODEL_WINDOW_KEYS:
        raw_model = rl.get(src_key)
        if not isinstance(raw_model, dict):
            continue
        parsed = window_from(raw_model, 10080, percent_keys)
        if parsed:
            model_weeks.append({"model": label, "week": parsed})
    if model_weeks:
        out["model_weeks"] = model_weeks
    return out


def freshen_window(window: Any, now: int) -> Any:
    """Drop a window's stale snapshot once its reset time has passed.

    Codex/Claude usage records are read from persisted session logs, so the most
    recent on-disk snapshot can predate the current window. When a window's
    ``resets_at`` is already in the past, the window has rolled over since the
    snapshot: quota is fully restored (used -> 0, i.e. 100% remaining) and the old
    reset time is meaningless (the next reset is unknown until the window is used
    again). This mirrors what the Codex/Claude CLIs themselves show after a reset.
    """
    if not isinstance(window, dict):
        return window
    epoch = parse_epoch(window.get("resets_at"))
    if epoch is not None and epoch <= now:
        out = dict(window)
        out["used"] = 0.0
        out["resets_at"] = None
        return out
    return window


def freshen_provider_windows(obj: Any, env: dict[str, str] | None = None) -> Any:
    """Apply :func:`freshen_window` to every window in a normalized provider dict."""
    if not isinstance(obj, dict):
        return obj
    now = now_epoch(env)
    for key in ("five_hour", "week"):
        if key in obj:
            obj[key] = freshen_window(obj.get(key), now)
    model_weeks = obj.get("model_weeks")
    if isinstance(model_weeks, list):
        for entry in model_weeks:
            if isinstance(entry, dict) and "week" in entry:
                entry["week"] = freshen_window(entry.get("week"), now)
    rows = obj.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                for key in ("five_hour", "week"):
                    if key in row:
                        row[key] = freshen_window(row.get(key), now)
    return obj


def local_snapshot_max_age(env: dict[str, str] | None = None) -> int:
    env = env or os.environ
    raw = env.get("LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE", str(DEFAULT_LOCAL_SNAPSHOT_MAX_AGE_SECONDS))
    try:
        parsed = int(float(raw or str(DEFAULT_LOCAL_SNAPSHOT_MAX_AGE_SECONDS)))
    except ValueError:
        return DEFAULT_LOCAL_SNAPSHOT_MAX_AGE_SECONDS
    if parsed <= 0:
        return DEFAULT_LOCAL_SNAPSHOT_MAX_AGE_SECONDS
    return min(DEFAULT_LOCAL_SNAPSHOT_MAX_AGE_SECONDS, parsed)


def local_snapshot_is_stale(source_mtime: int | None, env: dict[str, str] | None = None) -> bool:
    if source_mtime is None:
        return False
    max_age = local_snapshot_max_age(env)
    return now_epoch(env) - source_mtime > max_age


def provider_snapshot_requires_fresh_source(obj: Any, env: dict[str, str] | None = None) -> bool:
    """Return true when a local snapshot still claims an active/unknown window.

    If every reset-bound window already elapsed, the old sample can be safely
    freshened to full quota. Otherwise, an old local file may materially
    under/over-report current usage and should not be displayed as current data.
    """
    if not isinstance(obj, dict):
        return False
    now = now_epoch(env)

    def active_or_unknown(window: Any) -> bool:
        if not isinstance(window, dict):
            return False
        epoch = parse_epoch(window.get("resets_at"))
        return epoch is None or epoch > now

    for key in ("five_hour", "week"):
        if active_or_unknown(obj.get(key)):
            return True
    rows = obj.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("five_hour", "week"):
                if active_or_unknown(row.get(key)):
                    return True
    return False


def stale_usage_provider(provider: str, source: str, source_mtime: int | None, env: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "provider": provider,
        "source": source,
        "available": False,
        "reason": "stale-usage",
        "source_mtime": source_mtime,
        "stale_after_seconds": local_snapshot_max_age(env),
    }


def stale_if_local_snapshot(
    provider: str,
    obj: dict[str, Any] | None,
    source: str,
    source_mtime: int | None,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if obj is None:
        return None
    if local_snapshot_is_stale(source_mtime, env) and provider_snapshot_requires_fresh_source(obj, env):
        return stale_usage_provider(provider, source, source_mtime, env)
    return obj


def is_stale_usage_result(obj: Any) -> bool:
    """True when a provider read degraded to the ``stale-usage`` marker."""
    return isinstance(obj, dict) and obj.get("available") is False and obj.get("reason") == "stale-usage"


def stale_recovery_schedule(env: dict[str, str] | None = None) -> list[float]:
    """Backoff delays (seconds) for re-driving a live read that would otherwise
    surface ``stale-usage``.

    Returns an empty schedule when active-refresh retries are globally disabled
    (``LLM_USAGE_LIVE_FETCH_RETRIES=0`` — the value the test-suite pins) so
    failure-path tests never sleep. Otherwise a bounded exponential backoff: it
    bridges the network-settling window after a resume-from-suspend (the common
    cause of a transient live-fetch failure) without stalling the watch loop
    indefinitely when a provider is genuinely offline.
    """
    env = env or os.environ
    try:
        base_retries = int(env.get("LLM_USAGE_LIVE_FETCH_RETRIES", "2") or "2")
    except ValueError:
        base_retries = 2
    if base_retries <= 0:
        return []
    try:
        attempts = max(0, int(env.get("LLM_USAGE_STALE_RECOVERY_ATTEMPTS", "4") or "4"))
    except ValueError:
        attempts = 4
    try:
        base_delay = max(0.0, float(env.get("LLM_USAGE_STALE_RECOVERY_DELAY", "0.5") or "0.5"))
    except ValueError:
        base_delay = 0.5
    try:
        max_delay = max(0.0, float(env.get("LLM_USAGE_STALE_RECOVERY_MAX_DELAY", "4.0") or "4.0"))
    except ValueError:
        max_delay = 4.0
    schedule: list[float] = []
    delay = base_delay
    for _ in range(attempts):
        schedule.append(min(delay, max_delay))
        delay *= 2
    return schedule


def recover_stale_with_live_retry(
    read_fn: "Any",
    live_available: bool,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Re-run ``read_fn`` with bounded backoff while it yields ``stale-usage``.

    A ``stale-usage`` result means the on-disk snapshot is too old to trust *and*
    the proactive live read failed on this attempt. As long as a live
    acquisition path exists (credentials / CLI present), surfacing stale data to
    the user is the wrong outcome — the whole point of the tool is to obtain
    current usage proactively. So we keep re-driving the live read, giving a
    transient outage (typically the network stack still settling right after a
    resume from suspend) time to clear before we ever fall back to stale data.

    The retry budget is bounded two ways — a maximum attempt count
    (:func:`stale_recovery_schedule`) and a wall-clock deadline
    (``LLM_USAGE_STALE_RECOVERY_MAX_WAIT``, since each re-drive carries its own
    network timeout) — so a provider that is genuinely unreachable still returns
    promptly without stalling the watch loop, and ``stale-usage`` only reaches
    the user under truly exceptional circumstances. When no live path exists, the
    first (stale) result is returned immediately.
    """
    env = env or os.environ
    result = read_fn()
    if not live_available or not is_stale_usage_result(result):
        return result
    deadline = time.monotonic() + _stale_recovery_max_wait(env)
    for delay in stale_recovery_schedule(env):
        if time.monotonic() >= deadline:
            break
        if delay > 0:
            time.sleep(delay)
        result = read_fn()
        if not is_stale_usage_result(result):
            return result
    return result


def _stale_recovery_max_wait(env: dict[str, str] | None = None) -> float:
    env = env or os.environ
    try:
        value = float(env.get("LLM_USAGE_STALE_RECOVERY_MAX_WAIT", "20") or "20")
    except ValueError:
        return 20.0
    return value if value > 0 else 20.0


def latest_matching_record(root: Path, predicate: Any, env: dict[str, str] | None = None) -> tuple[str, Path, int] | None:
    env = env or os.environ
    if not root.is_dir():
        return None
    max_files = int(env.get("LLM_USAGE_MAX_FILES", "250") or "250")
    tail_lines = int(env.get("LLM_USAGE_TAIL_LINES", "2000") or "2000")
    files = [
        p
        for p in root.rglob("*")
        if p.is_file() and (p.name.endswith(".jsonl") or (p.name.startswith("rollout-") and p.name.endswith(".jsonl")))
    ]
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    for path in files[:max_files]:
        try:
            mtime = int(path.stat().st_mtime)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:]
        except OSError:
            continue
        for line in reversed(lines):
            obj = read_json_text(line)
            if obj is not None and predicate(obj):
                return line, path, mtime
    return None


def latest_matching_line(root: Path, predicate: Any, env: dict[str, str] | None = None) -> str | None:
    record = latest_matching_record(root, predicate, env)
    return record[0] if record else None


def read_codex(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    from .providers import codex as _codex_provider

    return _codex_provider.read_codex(env)


def codex_cli(env: dict[str, str] | None = None) -> str | None:
    env = env or os.environ
    return shutil.which("codex", path=env.get("PATH"))


def _codex_has_auth(env: dict[str, str]) -> bool:
    """True when Codex has usable credentials on disk.

    Mirrors the Claude OAuth pre-check: if there is no API key and no stored
    ChatGPT token, the app-server cannot answer and we should report
    ``not-authenticated`` rather than silently degrading to a stale snapshot.
    """
    auth = home_dir(env) / ".codex" / "auth.json"
    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    if str(data.get("OPENAI_API_KEY") or "").strip():
        return True
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        return any(str(tokens.get(k) or "").strip() for k in ("access_token", "id_token", "refresh_token"))
    return False


def codex_live_available(env: dict[str, str] | None = None) -> bool:
    """True when a live Codex rate-limit read can be attempted.

    Used to decide whether a ``stale-usage`` result is worth retrying: when the
    app-server can be reached on our own (CLI installed + authenticated, or a
    test payload is injected) a transient failure should be re-driven rather
    than surfaced as stale data. When the app-server is disabled or
    unauthenticated there is nothing to retry.
    """
    env = env or os.environ
    if env.get("LLM_USAGE_CODEX_RATE_LIMITS_JSON") is not None:
        return True
    if env.get("LLM_USAGE_DISABLE_CODEX_APP_SERVER") == "1":
        return False
    return bool(codex_cli(env)) and _codex_has_auth(env)


def _codex_app_server_timeout(env: dict[str, str]) -> float:
    raw = env.get("LLM_USAGE_CODEX_TIMEOUT", str(DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS))
    try:
        value = float(raw or DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS)
    except ValueError:
        return float(DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS)
    return value if value > 0 else float(DEFAULT_CODEX_APP_SERVER_TIMEOUT_SECONDS)


def _terminate_app_server(proc: "subprocess.Popen[str]") -> None:
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    except OSError:
        pass
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def _codex_app_server_rate_limits(env: dict[str, str]) -> tuple[dict[str, Any] | None, str | None]:
    """Fetch live Codex rate limits via the app-server JSON-RPC protocol.

    Returns ``(result, reason)`` where ``result`` is the raw
    ``account/rateLimits/read`` payload on success and ``reason`` describes a
    non-transient failure (``missing-cli`` / ``not-authenticated``). A
    transient failure (timeout, crash, network) returns ``(None, None)`` so the
    caller can fall back to the most recent on-disk snapshot.
    """
    injected = env.get("LLM_USAGE_CODEX_RATE_LIMITS_JSON")
    if injected is not None:
        try:
            obj = json.loads(injected)
        except json.JSONDecodeError:
            return None, None
        return (obj, None) if isinstance(obj, dict) else (None, None)
    if env.get("LLM_USAGE_DISABLE_CODEX_APP_SERVER") == "1":
        return None, None
    cli = codex_cli(env)
    if not cli:
        return None, "missing-cli"
    if not _codex_has_auth(env):
        return None, "not-authenticated"
    override = env.get("LLM_USAGE_CODEX_APP_SERVER_CMD")
    argv = shlex.split(override) if override else [cli, "app-server"]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
    except OSError:
        return None, None

    result: dict[str, Any] = {}
    flags = {"auth": False}
    done = threading.Event()

    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == 2:
                    payload = msg.get("result")
                    if isinstance(payload, dict):
                        result.update(payload)
                    elif "error" in msg:
                        text = json.dumps(msg.get("error") or {}).lower()
                        if any(t in text for t in ("auth", "login", "401", "unauthor", "credential")):
                            flags["auth"] = True
                    done.set()
                    return
        except (OSError, ValueError):
            pass
        finally:
            done.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "llm-tools", "version": "0.1.0"}}}) + "\n")
        proc.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        proc.stdin.write(json.dumps({"id": 2, "method": "account/rateLimits/read", "params": None}) + "\n")
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    done.wait(timeout=_codex_app_server_timeout(env))
    _terminate_app_server(proc)
    if result:
        return result, None
    if flags["auth"]:
        return None, "not-authenticated"
    return None, None


def codex_rate_limits_to_wire(result: dict[str, Any] | None, source: str) -> dict[str, Any] | None:
    """Translate an app-server ``account/rateLimits/read`` payload into the
    legacy Codex wire format (``five_hour`` / ``week`` / ``rows``).

    The payload carries a backward-compatible single bucket in ``rateLimits``
    plus a per-``limit_id`` view in ``rateLimitsByLimitId`` (where the Spark
    model shows up as its own bucket). We fold the Spark bucket back under a
    ``spark`` key so :func:`normalize_codex_obj` produces the same
    ``codex`` / ``codex-spark`` rows the rest of the codebase expects.
    """
    if not isinstance(result, dict):
        return None
    bucket = result.get("rateLimits")
    by_id = result.get("rateLimitsByLimitId")
    rl: dict[str, Any] = {}
    if isinstance(bucket, dict):
        rl.update(bucket)
    if isinstance(by_id, dict):
        if not rl and isinstance(by_id.get("codex"), dict):
            rl.update(by_id["codex"])
        for limit_id, sub in by_id.items():
            if not isinstance(sub, dict):
                continue
            name = str(sub.get("limitName") or "").lower()
            if "spark" in name or "spark" in str(limit_id).lower():
                rl["spark"] = sub
                break
    if not rl:
        return None
    return normalize_codex_obj({"rate_limits": rl}, source)


def read_codex_api(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Active Codex refresh via the app-server, with a cached fallback.

    On success the live payload is cached to ``codex-usage-api.json`` and the
    normalized snapshot is returned. A known authentication or CLI startup
    problem returns an ``available=False`` snapshot carrying that reason (so the
    caller surfaces it instead of stale data). A transient failure returns the
    cached payload when it is still fresh, otherwise ``None`` so the caller can
    fall back to the local Codex session logs.
    """
    env = env or os.environ
    cache = usage_cache_dir(env) / "codex-usage-api.json"
    result, reason = _codex_app_server_rate_limits(env)
    if result is not None:
        norm = codex_rate_limits_to_wire(result, "codex app-server")
        if norm:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(result, separators=(",", ":")) + "\n", encoding="utf-8")
            except OSError:
                pass
            return norm
    if reason in ("missing-cli", "not-authenticated"):
        return {"provider": "codex", "source": "codex app-server", "available": False, "reason": reason}
    if cache.is_file() and cache.stat().st_size > 0:
        try:
            mtime = int(cache.stat().st_mtime)
            cached = json.loads(cache.read_text(encoding="utf-8"))
            norm = codex_rate_limits_to_wire(cached, "codex app-server (cached)")
            if norm and stale_if_local_snapshot("codex", norm, "codex app-server (cached)", mtime, env) is norm:
                return norm
        except (OSError, json.JSONDecodeError):
            pass
    return None


def _read_claude_api_raw(env: dict[str, str] | None) -> dict[str, Any] | None:
    """Internal Claude API read; lives in common so provider adapters stay
    cyclic-free."""
    env = env or os.environ
    cache = usage_cache_dir(env) / "claude-usage-api.json"
    cred = home_dir(env) / ".claude" / ".credentials.json"
    cred_data: dict[str, Any] = {}
    try:
        parsed = json.loads(cred.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            cred_data = parsed
    except OSError:
        pass
    except json.JSONDecodeError:
        pass
    oauth = cred_data.get("claudeAiOauth")
    token = oauth.get("accessToken", "") if isinstance(oauth, dict) else ""
    if token:
        text, unauthorized = _fetch_claude_oauth_usage_text(token, env)
        if text:
            return _cache_claude_usage_response(cache, text)
        if unauthorized:
            refreshed = _refresh_claude_oauth_access_token(cred, cred_data, env)
            if refreshed:
                text, _ = _fetch_claude_oauth_usage_text(refreshed, env)
                if text:
                    return _cache_claude_usage_response(cache, text)
    elif isinstance(oauth, dict) and oauth.get("refreshToken"):
        refreshed = _refresh_claude_oauth_access_token(cred, cred_data, env)
        if refreshed:
            text, _ = _fetch_claude_oauth_usage_text(refreshed, env)
            if text:
                return _cache_claude_usage_response(cache, text)
    if cache.is_file() and cache.stat().st_size > 0:
        try:
            mtime = int(cache.stat().st_mtime)
            norm = normalize_claude_obj(json.loads(cache.read_text(encoding="utf-8")), str(cache))
            if stale_if_local_snapshot("claude", norm, str(cache), mtime, env) is not norm:
                return None
            return norm
        except (OSError, json.JSONDecodeError):
            return None
    return None


def live_fetch_retry_plan(env: dict[str, str] | None = None) -> tuple[int, float]:
    """(attempts, delay_seconds) for active-refresh network reads.

    Active refresh is what keeps usage fresh; a single transient failure (e.g. a
    network stack still settling right after a resume from suspend) must not
    immediately degrade a provider to a stale on-disk snapshot. A couple of
    bounded retries turn most of those blips into fresh data. Tests pin
    ``LLM_USAGE_LIVE_FETCH_RETRIES=0`` to stay single-shot and hermetic.
    """
    env = env or os.environ
    try:
        attempts = max(1, int(env.get("LLM_USAGE_LIVE_FETCH_RETRIES", "2") or "2") + 1)
    except ValueError:
        attempts = 3
    try:
        delay = max(0.0, float(env.get("LLM_USAGE_LIVE_FETCH_RETRY_DELAY", "0.5") or "0.5"))
    except ValueError:
        delay = 0.5
    return attempts, delay


def live_fetch_retry_max_delay(env: dict[str, str] | None = None) -> float:
    """Cap (seconds) on a server-requested ``Retry-After`` back-off.

    A 429 carries a ``Retry-After`` (the Claude OAuth usage endpoint asks for
    ~8s); honouring it on the same invocation turns a transient rate-limit into
    a fresh read instead of a silent degrade to ``unavailable``. The cap keeps a
    pathological ``Retry-After`` from hanging the tool. ``0`` disables the wait
    (tests pin it so the rate-limit path stays instant and hermetic).
    """
    env = env or os.environ
    try:
        return max(0.0, float(env.get("LLM_USAGE_LIVE_FETCH_RETRY_MAX_DELAY", "10") or "10"))
    except ValueError:
        return 10.0


def _retry_after_seconds(exc: HTTPError, env: dict[str, str], fallback: float) -> float:
    """Seconds to wait before retrying a 429, from ``Retry-After`` (capped)."""
    raw = None
    try:
        raw = exc.headers.get("Retry-After")
    except AttributeError:
        raw = None
    wait = fallback
    if raw:
        try:
            wait = float(str(raw).strip())
        except (TypeError, ValueError):
            wait = fallback
    return max(0.0, min(wait, live_fetch_retry_max_delay(env)))


def _fetch_claude_oauth_usage_text(access_token: str, env: dict[str, str] | None = None) -> tuple[str | None, bool]:
    env = env or os.environ
    req = Request(
        CLAUDE_OAUTH_USAGE_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": CLAUDE_OAUTH_BETA,
            "User-Agent": CLAUDE_OAUTH_USER_AGENT,
        },
    )
    attempts, delay = live_fetch_retry_plan(env)
    # A rate-limit (429) is transient and authoritative about *when* to retry, so
    # it gets one extra honoured-``Retry-After`` attempt on top of the network
    # budget rather than being lumped in with bad-token 4xx. Without this a 429
    # used to degrade straight to the ~/.claude/projects fallback (which reports
    # ``unavailable``), so any second read inside the endpoint's ~8s window made
    # Claude usage vanish from the dashboard. When retries are globally disabled
    # (``LLM_USAGE_LIVE_FETCH_RETRIES=0``, the test pin) the budget is 1 and the
    # rate-limit retry is suppressed too, so the path stays single-shot.
    rate_limit_retry_used = attempts <= 1
    attempt = 0
    while True:
        try:
            with urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", "replace"), False
        except HTTPError as exc:
            # 400/401 are authoritative (bad/expired token): do not retry, let the
            # caller refresh the OAuth token instead.
            if exc.code in {400, 401}:
                return None, True
            if exc.code == 429:
                # Honour Retry-After exactly once. Burning the quick
                # network-retry budget on a 429 is pointless (0.5s later it is
                # still rate-limited) and only adds load to an endpoint that
                # already asked us to back off, so give up after the one wait.
                if rate_limit_retry_used:
                    return None, False
                rate_limit_retry_used = True
                wait = _retry_after_seconds(exc, env, delay)
                if wait:
                    time.sleep(wait)
                continue
            if 400 <= exc.code < 500:
                # Any other 4xx is a client error that will not change on retry.
                return None, False
            # 5xx is a transient server error: fall through to the bounded
            # network-retry budget below.
        except Exception:
            pass
        attempt += 1
        if attempt < attempts:
            # Retry whenever budget remains; the delay only gates the sleep so
            # tests can pin it to 0 and still exercise the retry path.
            if delay:
                time.sleep(delay)
            continue
        return None, False


def _cache_claude_usage_response(cache: Path, text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    return normalize_claude_obj(json.loads(text), "api.anthropic.com/api/oauth/usage")


def _refresh_claude_oauth_access_token(cred_path: Path, cred_data: dict[str, Any], env: dict[str, str] | None = None) -> str | None:
    oauth = cred_data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    refresh_token = str(oauth.get("refreshToken") or "").strip()
    if not refresh_token:
        return None
    payload = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_OAUTH_CLIENT_ID,
        }
    ).encode("utf-8")
    req = Request(
        CLAUDE_OAUTH_TOKEN_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": CLAUDE_OAUTH_USER_AGENT,
        },
    )
    # The refresh POST is the critical step after a long suspend (the access
    # token has usually expired by then). It must be at least as resilient as
    # the usage fetch itself: a single transient failure here previously forced
    # a degrade to the stale on-disk cache. A 4xx is authoritative (the refresh
    # token is bad) and is not retried.
    attempts, delay = live_fetch_retry_plan(env)
    raw: Any = None
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                return None
            if attempt + 1 < attempts and delay:
                time.sleep(delay)
        except Exception:
            if attempt + 1 < attempts and delay:
                time.sleep(delay)
    if not isinstance(raw, dict):
        return None
    access_token = str(raw.get("access_token") or "").strip()
    if not access_token:
        return None
    oauth["accessToken"] = access_token
    next_refresh = str(raw.get("refresh_token") or "").strip()
    if next_refresh:
        oauth["refreshToken"] = next_refresh
    expires_in = num(raw.get("expires_in"))
    if expires_in is not None and expires_in > 0:
        oauth["expiresAt"] = int((time.time() + expires_in) * 1000)
    try:
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(json.dumps(cred_data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return access_token


def read_claude_api(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    return _read_claude_api_raw(env)


def _claude_has_auth(env: dict[str, str] | None = None) -> bool:
    """True when Claude has OAuth credentials that can drive a live usage fetch.

    Mirrors :func:`_codex_has_auth`: a present access *or* refresh token means
    the OAuth usage endpoint is reachable on our own (we can refresh an expired
    access token), so a transient failure must be retried rather than degraded
    to a stale snapshot. No credentials means the live path is genuinely
    unavailable and there is nothing to retry.
    """
    env = env or os.environ
    cred = home_dir(env) / ".claude" / ".credentials.json"
    try:
        data = json.loads(cred.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return False
    return bool(str(oauth.get("accessToken") or "").strip() or str(oauth.get("refreshToken") or "").strip())


def read_claude(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = env or os.environ
    live_available = _claude_has_auth(env)
    return recover_stale_with_live_retry(
        lambda: freshen_provider_windows(_read_claude_raw(env), env),
        live_available,
        env,
    )


def _read_claude_raw(env: dict[str, str]) -> dict[str, Any] | None:
    api = read_claude_api(env)
    if api:
        return api
    status_cache = usage_cache_dir(env) / "claude-status.json"
    last_stale: dict[str, Any] | None = None
    if status_cache.is_file() and status_cache.stat().st_size > 0:
        try:
            mtime = int(status_cache.stat().st_mtime)
            source = str(status_cache)
            norm = normalize_claude_obj(json.loads(status_cache.read_text(encoding="utf-8")), source)
            if norm:
                stale = stale_if_local_snapshot("claude", norm, source, mtime, env)
                if stale is not norm:
                    last_stale = stale
                else:
                    return norm
        except (OSError, json.JSONDecodeError):
            pass
    root = home_dir(env) / ".claude" / "projects"
    record = latest_matching_record(root, lambda o: get_path(o, (("rate_limits",), ("rateLimits",), ("message", "rate_limits"), ("message", "rateLimits"))) is not None, env)
    if not record:
        return last_stale
    line, _path, mtime = record
    source = "~/.claude/projects"
    norm = normalize_claude_obj(json.loads(line), source)
    stale = stale_if_local_snapshot("claude", norm, source, mtime, env)
    if stale is not norm:
        return stale
    return norm


def find_copilot_cli(env: dict[str, str] | None = None) -> str | None:
    env = env or os.environ
    path = env.get("PATH")
    return shutil.which("copilot", path=path) or shutil.which("github-copilot", path=path)


def copilot_config_dir(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    return Path(env.get("COPILOT_HOME") or (Path.home() / ".copilot"))


# Footer items we screen-scrape are off by default on a fresh Copilot install and
# only appear once the user enables them via /statusline. We seed them so usage is
# visible without any manual setup. "quota" drives "Plan: N% used" (and legacy
# premium requests); "ai-used" drives "Session: N AIC used".
COPILOT_REQUIRED_FOOTER_KEYS = ("showQuota", "showAiUsed")


def ensure_copilot_footer_settings(env: dict[str, str] | None = None) -> None:
    """Make sure the Copilot footer exposes the quota/usage items we parse.

    Non-destructive: only the required footer flags are flipped to true, every
    other user setting is preserved, and the file is rewritten only when a flag
    actually changes. Any failure is swallowed so capture never breaks.
    """
    env = env or os.environ
    if env.get("LLM_USAGE_COPILOT_NO_SETTINGS_WRITE", "0") == "1":
        return
    settings_path = copilot_config_dir(env) / "settings.json"
    try:
        data: dict[str, Any] = {}
        if settings_path.is_file():
            raw = settings_path.read_text(encoding="utf-8").strip()
            if raw:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    # Unexpected shape: leave the user's file untouched.
                    return
                data = parsed
        footer = data.get("footer")
        if not isinstance(footer, dict):
            footer = {}
        changed = False
        for key in COPILOT_REQUIRED_FOOTER_KEYS:
            if footer.get(key) is not True:
                footer[key] = True
                changed = True
        if not changed:
            return
        data["footer"] = footer
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_name(f"{settings_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(settings_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return


def strip_ansi(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-9;?<>=-]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b[()][\x20-\x7e]", "", text)
    text = re.sub(r"\x1b[@-Z\\^_<>=78]", "", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\a", "").replace("\x0e", "").replace("\x0f", "")


def capture_copilot_screen(env: dict[str, str] | None = None) -> tuple[str, str]:
    env = env or os.environ
    if env.get("LLM_USAGE_DISABLE_COPILOT", "0") == "1":
        return "disabled", ""
    if "LLM_USAGE_COPILOT_CAPTURE_TEXT" in env:
        return "fixture", env.get("LLM_USAGE_COPILOT_CAPTURE_TEXT", "")
    cli = find_copilot_cli(env)
    if not cli:
        return "missing-cli", ""
    capture_cwd = env.get("LLM_USAGE_COPILOT_CWD") or str(Path(__file__).resolve().parent.parent)
    helper_cmd = env.get("LLM_USAGE_COPILOT_CAPTURE_CMD", "")
    timeout_seconds = int(env.get("LLM_USAGE_COPILOT_TIMEOUT", "10") or "10")
    if not helper_cmd:
        # The real CLI only renders the quota/usage footer when these items are
        # enabled, so seed them before launching (no-op once already on).
        ensure_copilot_footer_settings(env)
    argv = ["bash", "-lc", helper_cmd] if helper_cmd else [cli, "--screen-reader", "-C", capture_cwd]
    try:
        status, output = run_pty_capture(
            argv,
            Path(capture_cwd),
            timeout_seconds,
            stream=False,
            auto_confirm=True,
            detect_prompts=False,
        )
    except Exception:
        return "capture-error", ""
    if status == 124:
        capture_status = "timeout"
    elif status == 0:
        capture_status = "ok"
    else:
        capture_status = "capture-error"
    text = strip_ansi(output)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return capture_status, text


def parse_copilot_monthly_used(text: str) -> float | None:
    """Pull the monthly "Plan N% used" / "Remaining N%" figure from a Copilot
    CLI footer snapshot.

    The footer shape has shifted across releases:

    * Older CLIs (pre-1.0.57): ``Plan: 62% used`` or ``Monthly: 62% used``.
    * 1.0.57+ render the **remaining** percentage (``Remaining reqs.: 59%``)
      instead of the used percentage, because GitHub bills by what's left of
      the monthly allowance. The user-facing metric the dashboard has always
      reported is "remaining", so we flip that back to "used" here.
    """
    m = re.search(r"(?:Monthly|Plan):\s*([0-9]+(?:[.][0-9]+)?)%\s*used", text)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:Remaining\s+reqs?\.?|Remaining\s+requests?):\s*([0-9]+(?:[.][0-9]+)?)\s*%", text)
    if m:
        return max(0.0, min(100.0, 100.0 - float(m.group(1))))
    return None


def parse_copilot_ai_credits(text: str) -> float | None:
    """Pull the per-session "AI Credits" used figure from a Copilot footer.

    Both the legacy colon form (``AI Credits: 7.41``) and the new
    space-then-value form (``AI Credits 7.41 (8s)``) are accepted; the
    parenthetical elapsed time and any trailing unit (``s``/``m``) are
    ignored. The two field shapes coexist across releases so the parser
    has to recognise both.
    """
    m = re.search(r"AI\s+Credits:\s*([0-9]+(?:[.][0-9]+)?)", text)
    if m:
        return float(m.group(1))
    m = re.search(r"AI\s+Credits\s+([0-9]+(?:[.][0-9]+)?)", text)
    return float(m.group(1)) if m else None


def copilot_capture_block_reason(status: str, screen: str) -> str | None:
    """Reason that should prevent billing fallback from marking Copilot usable."""
    if status in {"disabled", "missing-cli"}:
        return status
    if "trust_prompt_seen" in screen:
        return "trust-prompt"
    if re.search(r"[Ll]og\s*-?\s*[Ii]n|[Aa]uth", screen):
        return "not-authenticated"
    return None


def read_copilot_live(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    status, screen = capture_copilot_screen(env)
    monthly_used = parse_copilot_monthly_used(screen)
    ai_credits = parse_copilot_ai_credits(screen)
    block_reason = copilot_capture_block_reason(status, screen)
    if block_reason is not None:
        return {"provider": "copilot", "source": "copilot cli", "available": False, "reason": block_reason}
    monthly_result: dict[str, Any] | None = None
    source = "copilot cli"
    if monthly_used is not None:
        monthly_result = {
            "used": monthly_used,
            "remaining": min(100.0, max(0.0, 100.0 - monthly_used)),
        }
    if monthly_result is None:
        # The new Copilot CLI (>= 1.0.57) no longer prints the
        # ``Plan: N% used`` figure in its footer — the only monthly signal
        # visible from inside the CLI is the per-session "AI Credits" counter
        # and the token summary, neither of which represents the user's
        # remaining monthly allowance. When the live footer is missing the
        # monthly figure, fall back to the GitHub
        # ``premium_request/usage`` REST endpoint, which always reports the
        # current month's premium request consumption and lets us draw a
        # real quota bar instead of collapsing to ``unavailable``. The
        # fallback is best-effort: when no GitHub token is available, no
        # user is logged in, or the API returns nothing for the current
        # month, we still report ``format-changed`` so the user knows the
        # live footer is the reason.
        monthly_fallback = read_copilot_monthly_used(env)
        if (
            isinstance(monthly_fallback, dict)
            and num(monthly_fallback.get("used")) is not None
        ):
            used = float(num(monthly_fallback["used"]))
            remaining = monthly_fallback.get("remaining")
            remaining_float = (
                float(num(remaining))
                if num(remaining) is not None
                else max(0.0, 100.0 - used)
            )
            monthly_result = {"used": used, "remaining": remaining_float}
            source = str(monthly_fallback.get("source") or "github billing")
    if monthly_result is not None or ai_credits is not None:
        return {
            "provider": "copilot",
            "source": source,
            "capture_status": status,
            "monthly": monthly_result,
            "ai_credits": None if ai_credits is None else {"used": ai_credits},
        }
    reason = "format-changed" if screen else status
    return {"provider": "copilot", "source": "copilot cli", "available": False, "reason": reason}


def copilot_refresh_wait_budget(env: dict[str, str], cache_present: bool) -> float:
    # We only reach this path when the cache is stale or missing (a fresh cache
    # returns earlier). In both cases the goal is the same: refresh and show
    # CURRENT data, never a snapshot already past its TTL. So wait long enough
    # for the in-flight capture to land. The wait loop returns as soon as a fresh
    # snapshot appears, so a fast capture still returns fast; only a slow capture
    # pays the full budget. ``cache_present`` is retained for call-site clarity
    # but no longer shortens the wait -- a warm-but-stale cache used to wait ~1s
    # and serve the old value, which is exactly the staleness we are removing.
    _ = cache_present
    try:
        timeout = int(env.get("LLM_USAGE_COPILOT_TIMEOUT", "10") or "10")
    except ValueError:
        timeout = 10
    # The detached refresh first runs the PTY capture (up to
    # ``LLM_USAGE_COPILOT_TIMEOUT`` -- and on the new Copilot CLI, which no longer
    # prints the ``Plan: N% used`` footer, that capture *always* burns the full
    # timeout) and only THEN runs the GitHub ``premium_request/usage`` fallback
    # that actually supplies the monthly figure. Under ``llm-usage``'s parallel
    # provider fan-out the GitHub leg competes for CPU/network, so the old
    # ``timeout + 2`` budget landed right on the boundary: the very first (cold)
    # run routinely gave up a beat before the refresh wrote the cache, dropping
    # the monthly bar *and* the add-on ``spend`` row (which is gated on
    # availability) and skewing the budget total. Size the budget to cover the
    # billing fallback so the cold run waits for real data. The wait loop returns
    # as soon as the cache lands (or the refresh lock clears), so this only moves
    # the give-up threshold -- it does not slow the happy path.
    try:
        fallback = int(env.get("LLM_USAGE_COPILOT_BILLING_FALLBACK_BUDGET", "8") or "8")
    except ValueError:
        fallback = 8
    default = str(timeout + max(0, fallback))
    raw = env.get("LLM_USAGE_COPILOT_REFRESH_WAIT", default) or default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(default)


def read_copilot(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    cache = usage_cache_dir(env) / "copilot-usage.json"
    lock = usage_cache_dir(env) / "copilot-refresh.lock"
    ignored_transient_mtime: int | None = None

    def cached_result(allow_ignored_transient: bool = False) -> dict[str, Any] | None:
        nonlocal ignored_transient_mtime
        if not cache.is_file() or cache.stat().st_size <= 0:
            return None
        mtime = int(cache.stat().st_mtime)
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            isinstance(data, dict)
            and data.get("available") is False
            and data.get("reason") in TRANSIENT_COPILOT_CACHE_REASONS
            and not allow_ignored_transient
        ):
            ignored_transient_mtime = mtime
            return None
        if ignored_transient_mtime is not None and mtime <= ignored_transient_mtime and not allow_ignored_transient:
            return None
        return data if isinstance(data, dict) else None

    def last_known_good() -> dict[str, Any] | None:
        """A cached snapshot that carries real usage numbers, regardless of age.

        Copilot's only data source is a slow, occasionally-flaky PTY capture, so
        a refresh can miss its wait budget (or be blocked by a dead lock). When
        that happens we still want to show the most recent monthly figure on
        start rather than drop to ``unavailable`` -- the spawned background
        refresh keeps the number current for the next run. Snapshots that merely
        record a failure (``available: false``) are not "good" and are skipped.
        """
        if not cache.is_file() or cache.stat().st_size <= 0:
            return None
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("available") is False:
            return None
        monthly = data.get("monthly")
        has_monthly = isinstance(monthly, dict) and monthly.get("remaining") is not None
        has_credits = data.get("ai_credits") is not None
        return data if (has_monthly or has_credits) else None

    bypass = (
        "LLM_USAGE_COPILOT_CAPTURE_TEXT" in env
        or bool(env.get("LLM_USAGE_COPILOT_CAPTURE_CMD"))
        or env.get("LLM_USAGE_DISABLE_COPILOT", "0") == "1"
        or env.get("LLM_USAGE_COPILOT_CACHE_TTL", "300") == "0"
    )
    if bypass:
        return read_copilot_live(env)
    ttl = int(env.get("LLM_USAGE_COPILOT_CACHE_TTL", "300") or "300")
    if cache.is_file() and cache.stat().st_size > 0 and int(time.time()) - int(cache.stat().st_mtime) <= ttl:
        cached = cached_result()
        if cached is not None:
            return cached
    refresh_started = False
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        lock.mkdir()
        refresh_started = True
    except FileExistsError:
        try:
            stale_after = int(env.get("LLM_USAGE_COPILOT_TIMEOUT", "10") or "10") + 5
            if int(time.time()) - int(lock.stat().st_mtime) > stale_after:
                lock.rmdir()
                lock.mkdir()
                refresh_started = True
        except OSError:
            pass
    except OSError:
        pass
    if refresh_started:
        refresh_env = dict(env)
        subprocess.Popen(
            [sys.executable, "-m", "llm_tools.copilot_refresh", str(cache)],
            env=refresh_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    wait_budget = copilot_refresh_wait_budget(env, cache.is_file() and cache.stat().st_size > 0)
    deadline = time.time() + wait_budget
    while time.time() < deadline:
        if cache.is_file() and cache.stat().st_size > 0 and int(time.time()) - int(cache.stat().st_mtime) <= ttl:
            cached = cached_result()
            if cached is not None:
                return cached
            if ignored_transient_mtime is None:
                break
        if not lock.exists():
            break
        time.sleep(0.05)
    # Only serve a snapshot that is still within its freshness window. A cache
    # already past TTL is stale and must not be shown -- the whole point of the
    # wait above was to let the refresh land current data. If it did not, fall
    # through to a synchronous live capture (below) or report refresh-pending,
    # rather than displaying numbers the caller would mistake for current.
    if cache.is_file() and cache.stat().st_size > 0 and int(time.time()) - int(cache.stat().st_mtime) <= ttl:
        cached = cached_result()
        if cached is not None:
            return cached
    if ignored_transient_mtime is not None and wait_budget > 0:
        live = read_copilot_live(env)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(live, separators=(",", ":")) + "\n", encoding="utf-8")
            tmp.replace(cache)
        except OSError:
            pass
        return live
    good = last_known_good()
    if good is not None:
        return good
    return {"provider": "copilot", "source": "copilot cli", "available": False, "reason": "refresh-pending"}


# --- Copilot figures via the GitHub billing API ----------------------------
#
# Two figures have to come from GitHub's billing REST API because the new
# Copilot CLI (>= 1.0.57) stopped printing them in its footer:
#
# 1. **Included-credit allowance** -- used to be ``Plan: N% used`` in the
#    footer; the live CLI now only shows the per-session "AI Credits" counter
#    and the token summary. ``/users/{login}/settings/billing/premium_request/
#    usage`` returns the current month's per-model premium request count,
#    which combined with the plan's monthly allowance gives us a
#    "used percent" the dashboard can render.
# 2. **Additional usage ($)** -- money charged beyond the included credit
#    allowance. The CLI never reported this, so we read it from
#    ``/users/{login}/settings/billing/usage`` (the ``netAmount`` line for
#    ``product == copilot``) and surface it as a "spent $X" row.
#
# Both readers reuse whatever GitHub token is already on the box -- the same
# token precedence the Copilot CLI itself documents (``COPILOT_GITHUB_TOKEN``
# > ``GH_TOKEN`` > ``GITHUB_TOKEN``), then ``gh auth token`` as a fallback.
# No new credential ever has to be configured for llm-usage; when no token is
# reachable the figure is simply omitted (or, in the included-allowance case,
# the row falls back to ``unavailable`` so the user sees the reason).
GITHUB_API_BASE = "https://api.github.com"


def _github_token(env: dict[str, str] | None = None) -> str | None:
    env = env or os.environ
    for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = str(env.get(key) or "").strip()
        if value:
            return value
    gh = shutil.which("gh", path=env.get("PATH"))
    if not gh:
        return None
    try:
        proc = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=8,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = (proc.stdout or "").strip()
    return token if proc.returncode == 0 and token else None


def _github_api_get(path: str, token: str, env: dict[str, str] | None = None) -> Any:
    url = path if path.startswith("http") else f"{GITHUB_API_BASE}{path}"
    req = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "llm-tools-usage",
        },
    )
    attempts, delay = live_fetch_retry_plan(env)
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except HTTPError as exc:
            # 4xx (bad/wrong-scoped token, missing user) is authoritative.
            if 400 <= exc.code < 500:
                return None
            if attempt + 1 < attempts and delay:
                time.sleep(delay)
        except Exception:
            if attempt + 1 < attempts and delay:
                time.sleep(delay)
    return None


def _copilot_addon_spent_from_usage(payload: Any, env: dict[str, str] | None = None) -> float | None:
    """Sum the current calendar month's Copilot *net* spend from a GitHub
    billing ``usage`` payload.

    GitHub reports the included allowance as a fully discounted line
    (``grossAmount == discountAmount``, ``netAmount == 0``); ``netAmount`` is the
    money charged *beyond* the allowance -- exactly the "Additional usage" figure.
    Items are month-bucketed, so we keep only the current month to mirror the
    web UI's per-cycle view.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("usageItems")
    if not isinstance(items, list):
        return None
    now = datetime.fromtimestamp(now_epoch(env), tz=timezone.utc)
    total = 0.0
    found = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("product") or "").lower() != "copilot":
            continue
        date = str(item.get("date") or "")
        # date is e.g. "2026-06-01T00:00:00Z"; keep the current month only.
        if len(date) >= 7:
            if date[:4] != f"{now.year:04d}" or date[5:7] != f"{now.month:02d}":
                continue
        net = num(item.get("netAmount"))
        if net is None:
            continue
        total += float(net)
        found = True
    if not found:
        return None
    return round(total, 2)


def _copilot_addon_cache_payload(cache: Path) -> dict[str, Any] | None:
    if not cache.is_file() or cache.stat().st_size <= 0:
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and num(data.get("spent")) is not None:
        return {"spent": float(num(data.get("spent"))), "currency": data.get("currency") or "$", "source": data.get("source") or "github billing (cached)"}
    return None


def read_copilot_addon(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Best-effort Copilot additional-usage ($) from the GitHub billing API.

    Returns ``{"spent": float, "currency": "$", "source": str}`` or ``None`` when
    the figure cannot be obtained (no token, network failure with no prior
    cache, billing API without Copilot items). The result is cached with a TTL;
    a transient failure degrades to the last cached value rather than dropping
    the figure, since monthly billing totals change slowly. This never blocks
    Copilot's primary (included-credit) reading: it is additive and isolated.
    """
    env = env or os.environ
    if env.get("LLM_USAGE_DISABLE_COPILOT_ADDON") == "1":
        return None
    # Test/diagnostic injection: a usage payload bypasses all network access.
    injected = env.get("LLM_USAGE_COPILOT_ADDON_USAGE_JSON")
    if injected is not None:
        spent = _copilot_addon_spent_from_usage(injected, env)
        return None if spent is None else {"spent": spent, "currency": "$", "source": "github billing"}
    cache = usage_cache_dir(env) / "copilot-addon.json"
    try:
        ttl = int(env.get("LLM_USAGE_COPILOT_ADDON_TTL", "900") or "900")
    except ValueError:
        ttl = 900
    if cache.is_file() and cache.stat().st_size > 0 and int(time.time()) - int(cache.stat().st_mtime) <= ttl:
        cached = _copilot_addon_cache_payload(cache)
        if cached is not None:
            return cached
    token = _github_token(env)
    if not token:
        return _copilot_addon_cache_payload(cache)
    login = str(env.get("LLM_USAGE_COPILOT_ADDON_LOGIN") or "").strip()
    if not login:
        user = _github_api_get("/user", token, env)
        login = str((user or {}).get("login") or "").strip() if isinstance(user, dict) else ""
    if not login:
        return _copilot_addon_cache_payload(cache)
    now = datetime.fromtimestamp(now_epoch(env), tz=timezone.utc)
    payload = _github_api_get(
        f"/users/{login}/settings/billing/usage?year={now.year}&month={now.month}",
        token,
        env,
    )
    spent = _copilot_addon_spent_from_usage(payload, env)
    if spent is None:
        return _copilot_addon_cache_payload(cache)
    result = {"spent": spent, "currency": "$", "source": "github billing"}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({**result, "ts": int(time.time())}, separators=(",", ":")) + "\n", encoding="utf-8")
        tmp.replace(cache)
    except OSError:
        pass
    return result


def decorate_window(window: dict[str, Any] | None) -> dict[str, Any] | None:
    if window is None:
        return None
    out = dict(window)
    out["remaining"] = remaining_from_used(out.get("used"))
    return out


# Monthly premium-request allowances per Copilot plan tier. The mapping is
# conservative: only the public per-user plans (Pro, Pro+, Business, Enterprise)
# are spelled out; unknown tiers fall back to the Pro default so we still
# surface a sensible "used vs allowance" ratio instead of refusing to draw a
# bar. Anyone with a custom allowance can override it explicitly with
# ``LLM_USAGE_COPILOT_MONTHLY_ALLOWANCE`` and skip the lookup.
_COPILOT_MONTHLY_ALLOWANCE_BY_PLAN: dict[str, int] = {
    "free": 50,
    "pro": 300,
    "pro_plus": 1500,
    "pro+": 1500,
    "business": 300,
    "enterprise": 1000,
}


def _copilot_monthly_allowance_for_plan(plan: str | None, env: dict[str, str] | None = None) -> int:
    """Return the per-month premium-request allowance for a given plan name.

    An explicit ``LLM_USAGE_COPILOT_MONTHLY_ALLOWANCE`` env override always
    wins so users with custom / enterprise contracts can pin the right
    denominator without code changes. Otherwise we look up the published
    allowance by the plan name (``pro`` → 300, ``pro_plus``/``pro+`` → 1500,
    etc.) and fall back to the Pro allowance when the plan is unknown — the
    alternative (refusing to render) leaves the user staring at a blank
    monthly cell.
    """
    env = env or os.environ
    raw = str(env.get("LLM_USAGE_COPILOT_MONTHLY_ALLOWANCE") or "").strip()
    if raw:
        try:
            value = int(float(raw))
            if value > 0:
                return value
        except ValueError:
            pass
    name = str(plan or "").strip().lower()
    if name in _COPILOT_MONTHLY_ALLOWANCE_BY_PLAN:
        return _COPILOT_MONTHLY_ALLOWANCE_BY_PLAN[name]
    # Free, unknown, or missing → 300 (Pro) keeps the math sane.
    return _COPILOT_MONTHLY_ALLOWANCE_BY_PLAN["pro"]


def _copilot_monthly_used_from_premium_request_usage(payload: Any) -> float | None:
    """Sum the current month's Copilot premium requests from a GitHub
    ``premium_request/usage`` payload.

    The endpoint returns one ``usageItems`` entry per (model, date) pair; the
    raw metric is in *requests* (per the ``unitType: requests`` field), and
    the ``grossQuantity`` field is the count of premium requests consumed
    against the user's monthly allowance. We sum across the current calendar
    month (the caller is expected to pass a payload already filtered to the
    current ``year``/``month``) and return the raw request count; the caller
    multiplies by the plan allowance to get a percent.

    Three outcomes are distinguished so the dashboard can render the right
    cell:

    * **API responded with no rows** (``usageItems`` is ``[]`` or has no
      Copilot product): the user has a paid plan but no recorded usage
      yet, so we return ``0.0`` and the dashboard draws a full "remaining"
      bar instead of collapsing to ``unavailable``.
    * **API responded with rows**: we sum the ``grossQuantity`` across
      Copilot items and return the request count.
    * **API did not respond / malformed** (``usageItems`` is missing, not a
      list, or the payload itself is not a JSON object): we return
      ``None`` so the caller can degrade to its own failure shape.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    items = payload.get("usageItems")
    if items is None:
        return None
    if not isinstance(items, list):
        return None
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("product") or "").lower() != "copilot":
            continue
        gross = num(item.get("grossQuantity"))
        if gross is None or gross <= 0:
            continue
        total += float(gross)
    return total


def read_copilot_monthly_used(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Best-effort Copilot monthly *used* percent from the GitHub billing API.

    The new Copilot CLI (>= 1.0.57) no longer prints the
    ``Plan: N% used`` figure in its footer; the dashboard needs a substitute
    so the monthly cell stays informative rather than ``unavailable``. The
    premium_request/usage endpoint exposes the raw per-model request counts
    for the current month, and combined with the plan's monthly allowance
    that gives us a "used percent" the dashboard can render as a quota bar.

    Returns ``{"used": float, "remaining": float, "requests": int,
    "allowance": int, "source": str}`` or ``None`` when no measurement is
    possible (no token, no plan, API failure with no usable cache). The
    result is cached with a short TTL because the figure is month-bucketed
    and changes at most once per request.

    Never raises: a billing-API failure is the whole reason the live
    ``Plan: N% used`` footer exists, so the fallback has to be similarly
    well-behaved.
    """
    env = env or os.environ
    if env.get("LLM_USAGE_DISABLE_COPILOT_MONTHLY") == "1":
        return None
    # Test/diagnostic injection: a usage payload bypasses all network access.
    injected = env.get("LLM_USAGE_COPILOT_PREMIUM_REQUEST_USAGE_JSON")
    if injected is not None:
        requests_used = _copilot_monthly_used_from_premium_request_usage(injected)
        if requests_used is None:
            return None
        allowance = _copilot_monthly_allowance_for_plan(
            env.get("LLM_USAGE_COPILOT_PLAN"), env
        )
        used_percent = max(0.0, min(100.0, requests_used / allowance * 100.0))
        return {
            "used": used_percent,
            "remaining": max(0.0, 100.0 - used_percent),
            "requests": int(round(requests_used)),
            "allowance": allowance,
            "source": "github billing",
        }
    cache = usage_cache_dir(env) / "copilot-monthly.json"
    try:
        ttl = int(env.get("LLM_USAGE_COPILOT_MONTHLY_TTL", "600") or "600")
    except ValueError:
        ttl = 600
    if cache.is_file() and cache.stat().st_size > 0 and int(time.time()) - int(cache.stat().st_mtime) <= ttl:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and num(cached.get("used")) is not None:
            return cached
    token = _github_token(env)
    if not token:
        return _copilot_monthly_cache_payload(cache)
    login = str(env.get("LLM_USAGE_COPILOT_ADDON_LOGIN") or "").strip()
    if not login:
        user = _github_api_get("/user", token, env)
        login = str((user or {}).get("login") or "").strip() if isinstance(user, dict) else ""
    if not login:
        return _copilot_monthly_cache_payload(cache)
    now = datetime.fromtimestamp(now_epoch(env), tz=timezone.utc)
    # The GitHub ``premium_request/usage`` endpoint is documented to only
    # return data for the past 24 hours, but in practice the
    # ``?year=...&month=...&day=...`` query returns full per-day
    # aggregates. The bare ``?year=...&month=...`` query (without a day)
    # silently returns an empty ``usageItems`` array for the *current*
    # month -- even when there is plenty of usage recorded for individual
    # days. This is a well-known API quirk: the dashboard has to ask for
    # the month one day at a time when computing the current calendar
    # month's premium request consumption, otherwise it draws a
    # full-green "headroom" bar on a day the user has actually exhausted
    # their allowance.
    is_current_month = True
    if "LLM_USAGE_COPILOT_PREMIUM_REQUEST_MONTH_OVERRIDE" in env:
        # Test seam: a frozen ``(year, month)`` lets a test pin "March 2026"
        # and assert the year+month fast path. The current-month day-by-day
        # loop is then skipped, so the test does not have to mock 30 API
        # responses just to exercise the sum logic.
        raw = env["LLM_USAGE_COPILOT_PREMIUM_REQUEST_MONTH_OVERRIDE"]
        try:
            parts = raw.split("-", 2)
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                now = now.replace(year=int(parts[0]), month=int(parts[1]), day=1)
                is_current_month = False
        except (ValueError, IndexError):
            pass
    if is_current_month:
        # Day-by-day sum for the current month so we see usage recorded
        # up to "today". The month-rollover is a wall-clock month
        # boundary, not 30 days back, so the iteration is bounded.
        # We fan out the per-day probes in parallel so a full-month
        # refresh does not take ``days * per-request-timeout``; the
        # bounded ``LLM_USAGE_PROVIDER_PARALLELISM`` (default = CPU
        # cores) keeps the fan-out from overwhelming the API.
        import calendar as _cal
        from concurrent.futures import ThreadPoolExecutor, as_completed

        days_in_month = _cal.monthrange(now.year, now.month)[1]
        end_day = min(now.day, days_in_month)
        day_results: dict[int, float | None] = {}

        def probe_day(day: int) -> tuple[int, float | None]:
            payload = _github_api_get(
                f"/users/{login}/settings/billing/premium_request/usage"
                f"?year={now.year}&month={now.month}&day={day}",
                token,
                env,
            )
            return day, _copilot_monthly_used_from_premium_request_usage(payload)

        try:
            parallelism = max(1, int(env.get("LLM_USAGE_PROVIDER_PARALLELISM", "4") or "4"))
        except ValueError:
            parallelism = 4
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = [pool.submit(probe_day, day) for day in range(1, end_day + 1)]
            for future in as_completed(futures):
                day, day_total = future.result()
                day_results[day] = day_total
        successful_days = [v for v in day_results.values() if v is not None]
        failed_days = len(successful_days) != len(day_results)
        requests_used = sum(successful_days)
        if requests_used == 0:
            # A paid plan with no recorded usage yet still legitimately
            # reports 0 used; fall back to a single month-level probe
            # in case the day-by-day responses all come back empty for
            # an unrelated reason (e.g. the token lost its scopes).
            payload = _github_api_get(
                f"/users/{login}/settings/billing/premium_request/usage"
                f"?year={now.year}&month={now.month}",
                token,
                env,
            )
            month_total = _copilot_monthly_used_from_premium_request_usage(payload)
            if month_total is not None:
                requests_used = month_total
            elif not successful_days or failed_days:
                requests_used = None
    else:
        payload = _github_api_get(
            f"/users/{login}/settings/billing/premium_request/usage?year={now.year}&month={now.month}",
            token,
            env,
        )
        requests_used = _copilot_monthly_used_from_premium_request_usage(payload)
    if requests_used is None:
        return _copilot_monthly_cache_payload(cache)
    plan = str(env.get("LLM_USAGE_COPILOT_PLAN") or "").strip()
    allowance = _copilot_monthly_allowance_for_plan(plan, env)
    used_percent = max(0.0, min(100.0, requests_used / allowance * 100.0))
    result: dict[str, Any] = {
        "used": used_percent,
        "remaining": max(0.0, 100.0 - used_percent),
        "requests": int(round(requests_used)),
        "allowance": allowance,
        "source": "github billing",
    }
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({**result, "ts": int(time.time())}, separators=(",", ":")) + "\n", encoding="utf-8")
        tmp.replace(cache)
    except OSError:
        pass
    return result


def _copilot_monthly_cache_payload(cache: Path) -> dict[str, Any] | None:
    if not cache.is_file() or cache.stat().st_size <= 0:
        return None
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or num(data.get("used")) is None:
        return None
    # Strip the bookkeeping "ts" key so the on-disk snapshot matches the
    # shape returned by a live call.
    return {k: v for k, v in data.items() if k != "ts"}


def json_for_provider(provider_json: dict[str, Any] | None, provider: str) -> dict[str, Any]:
    if not provider_json:
        return {"provider": provider, "available": False}
    out = dict(provider_json)
    if out.get("available") is False:
        out.setdefault("provider", provider)
        return out
    if isinstance(out.get("rows"), list) and out["rows"]:
        rows = []
        codex_row = None
        for row in out["rows"]:
            drow = {
                "key": row.get("key", ""),
                "name": row.get("name", ""),
                "source": row.get("source", out.get("source", "")),
                "five_hour": decorate_window(row.get("five_hour")),
                "week": decorate_window(row.get("week")),
            }
            rows.append(drow)
            if row.get("key") == "codex":
                codex_row = row
        out["available"] = True
        out["rows"] = rows
        out["five_hour"] = decorate_window((codex_row or {}).get("five_hour"))
        out["week"] = decorate_window((codex_row or {}).get("week"))
        return out
    out["available"] = True
    out["five_hour"] = decorate_window(out.get("five_hour"))
    out["week"] = decorate_window(out.get("week"))
    return out


def json_for_copilot(copilot_json: dict[str, Any] | None, show_credits: bool = False) -> dict[str, Any]:
    if not copilot_json:
        return {"provider": "copilot", "source": "copilot cli", "available": False, "reason": "unavailable"}
    out = dict(copilot_json)
    if not show_credits:
        out.pop("ai_credits", None)
    if out.get("available") is False:
        return out
    out["available"] = bool(out.get("monthly") or (show_credits and out.get("ai_credits")))
    return out


def log_usage_sample(provider: str, window: str, remaining: Any, env: dict[str, str] | None = None) -> None:
    if remaining in (None, "", "-", "unknown"):
        return
    path = usage_cache_dir(env) / "llm-usage.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps({"ts": now_epoch(env), "provider": provider, "window": window, "remaining": num(remaining)}, separators=(",", ":")) + "\n")
    except OSError:
        pass


def usage_log_tail_lines(env: dict[str, str]) -> int:
    try:
        return int(env.get("LLM_USAGE_LOG_TAIL_LINES", "50000") or "50000")
    except ValueError:
        return 50000


def prune_usage_log(env: dict[str, str] | None = None) -> None:
    env = env or os.environ
    path = usage_cache_dir(env) / "llm-usage.log"
    try:
        max_bytes = int(env.get("LLM_USAGE_LOG_MAX_BYTES", "10485760") or "10485760")
    except ValueError:
        max_bytes = 10485760
    if max_bytes <= 0:
        return
    try:
        if not path.is_file() or path.stat().st_size <= max_bytes:
            return
        with path.open("r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            lines = fh.read().splitlines()[-usage_log_tail_lines(env):]
            fh.seek(0)
            fh.truncate()
            fh.write("\n".join(lines) + ("\n" if lines else ""))
    except (OSError, UnicodeDecodeError):
        pass


# Approximate length of each usage window, used to require a minimum amount of
# observed history before extrapolating a window-scale burn-time estimate.
REMAINING_TIME_WINDOW_SECONDS = {
    "5h": 5 * 3600,
    "weekly": 7 * 24 * 3600,
    "monthly": 30 * 24 * 3600,
}


def estimate_remaining_seconds_from_log(provider: str, window: str, remaining: Any, env: dict[str, str] | None = None) -> int | None:
    env = env or os.environ
    rem = num(remaining)
    if rem is None or rem <= 0:
        return None
    path = usage_cache_dir(env) / "llm-usage.log"
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        max_stale = int(env.get("LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS", "600") or "600")
        lookback = int(env.get("LLM_USAGE_REMAINING_TIME_LOOKBACK_SECONDS", "259200") or "259200")
        max_gap = int(env.get("LLM_USAGE_REMAINING_TIME_MAX_GAP_SECONDS", "3600") or "3600")
    except ValueError:
        max_stale, lookback, max_gap = 600, 259200, 3600
    try:
        min_span_floor = int(env.get("LLM_USAGE_REMAINING_TIME_MIN_SPAN_SECONDS", "0") or "0")
    except ValueError:
        min_span_floor = 0
    try:
        min_span_fraction = float(env.get("LLM_USAGE_REMAINING_TIME_MIN_SPAN_FRACTION", "0.02") or "0.02")
    except ValueError:
        min_span_fraction = 0.02
    try:
        # A jump up in remaining of at least this many percentage points is a
        # window reset, not jitter -- it marks the start of the current window.
        reset_threshold = float(env.get("LLM_USAGE_REMAINING_TIME_RESET_THRESHOLD", "10") or "10")
    except ValueError:
        reset_threshold = 10.0
    try:
        # A drop steep enough to drain the entire window in less than this many
        # seconds is physically impossible -- treat it as a transient bad reading.
        min_drain_seconds = float(env.get("LLM_USAGE_REMAINING_TIME_MIN_DRAIN_SECONDS", "60") or "60")
    except ValueError:
        min_drain_seconds = 60.0
    now = now_epoch(env)
    cutoff = now - lookback
    samples: list[tuple[int, float]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-usage_log_tail_lines(env):]
    except OSError:
        return None
    for line in lines:
        obj = read_json_text(line)
        if not isinstance(obj, dict) or obj.get("provider") != provider or obj.get("window") != window:
            continue
        ts = num(obj.get("ts"))
        value = num(obj.get("remaining"))
        if ts is None or value is None or ts < cutoff:
            continue
        samples.append((int(ts), value))
    if not samples:
        return None
    if max_stale > 0 and now - samples[-1][0] > max_stale:
        return None
    # Reject isolated single-sample spikes: a reading that jumps away from BOTH
    # neighbours by at least reset_threshold and straight back (a V or inverted-V).
    # These are transient bad readings -- a momentarily stale or alternate-source
    # value mixed into the log -- and must not be mistaken for a window reset
    # (whose recovery would otherwise anchor here and discard all real history,
    # leaving too little span to estimate -> a spurious "no rate data"), nor
    # counted as real burn/recovery. A genuine reset rises and STAYS high, and a
    # sustained steep drop keeps dropping, so neither is removed here.
    if len(samples) >= 3:
        despiked: list[tuple[int, float]] = [samples[0]]
        for i in range(1, len(samples) - 1):
            prev_v, cur_v, next_v = samples[i - 1][1], samples[i][1], samples[i + 1][1]
            down_spike = prev_v - cur_v >= reset_threshold and next_v - cur_v >= reset_threshold
            up_spike = cur_v - prev_v >= reset_threshold and cur_v - next_v >= reset_threshold
            if not (down_spike or up_spike):
                despiked.append(samples[i])
        despiked.append(samples[-1])
        samples = despiked
    # Anchor to the CURRENT window: drop everything up to and including the most
    # recent reset (remaining jumping up by at least reset_threshold). Burn from
    # earlier windows -- and the transient glitches that cluster around an
    # exhausted one -- must not pollute this window's velocity, or a few old
    # spikes pin the ETA to a wildly short, "stuck" value that idle time can no
    # longer dilute. The same threshold gates the burn loop below, so a jump is
    # classified identically whether it anchors or merely interrupts a burn.
    anchor = 0
    for i in range(1, len(samples)):
        if samples[i][1] - samples[i - 1][1] >= reset_threshold:
            anchor = i
    samples = samples[anchor:]
    if len(samples) < 2:
        return None
    # Refuse to extrapolate a window-scale ETA from a sliver of history: a single
    # coarse step (e.g. a stale reading jumping to the current value) would
    # otherwise be read as a sustained burn rate and produce a wildly short
    # estimate. Require the observed span to cover a meaningful fraction of the
    # window before reporting anything.
    observed_span = samples[-1][0] - samples[0][0]
    window_seconds = REMAINING_TIME_WINDOW_SECONDS.get(window, 0)
    min_span = max(min_span_floor, int(window_seconds * min_span_fraction))
    if observed_span < min_span:
        return None
    prev_ts: int | None = None
    prev_rem = 0.0
    total_reduction = 0.0
    total_seconds = 0
    for ts, value in samples:
        if prev_ts is not None:
            dt = ts - prev_ts
            rise = value - prev_rem
            # A rise of at least reset_threshold is a window reset (same boundary
            # as the anchor above), and a gap longer than max_gap may hide one;
            # skip those intervals entirely. Otherwise count the elapsed time --
            # so idle stretches AND minor upward jitter both dilute the rate
            # rather than silently dropping their seconds -- and add a real drop's
            # burn, unless the drop is steep enough to drain the whole window in
            # under min_drain_seconds (a physically impossible, transient reading).
            if dt > 0 and (max_gap <= 0 or dt <= max_gap) and rise < reset_threshold:
                drop = prev_rem - value
                if drop > 0 and not (dt / drop * 100.0 < min_drain_seconds):
                    total_reduction += drop
                total_seconds += dt
        prev_ts = ts
        prev_rem = value
    if total_seconds <= 0 or total_reduction <= 0:
        return None
    remaining_seconds = int(rem * total_seconds / total_reduction)
    if remaining_seconds <= 0:
        return None
    return remaining_seconds


def estimate_remaining_time_from_log(provider: str, window: str, remaining: Any, env: dict[str, str] | None = None) -> str:
    remaining_seconds = estimate_remaining_seconds_from_log(provider, window, remaining, env)
    if remaining_seconds is None:
        return "-"
    if remaining_seconds < 60:
        return "1m"
    return fmt_duration(remaining_seconds)


def validate_prompt_args(prompt_text: str, prompt_file: str) -> None:
    if prompt_text and prompt_file:
        err("use exactly one of --prompt or --prompt-file")
        raise SystemExit(2)
    if not prompt_text and not prompt_file:
        err("one of --prompt or --prompt-file is required")
        raise SystemExit(2)
    if prompt_file and not os.access(prompt_file, os.R_OK):
        err(f"prompt file is not readable: {prompt_file}")
        raise SystemExit(2)


def validate_retry_delays(value: str) -> None:
    if not value:
        return
    if any(not is_integer(part) for part in value.split(",")):
        err("--retry-delays must be comma-separated integer seconds")
        raise SystemExit(2)


def validate_provider_window(provider: str, window: str) -> None:
    """Deprecated alias for :func:`validate_provider_scope`."""
    validate_provider_scope(provider, window)


def validate_provider_scope(provider: str, scope: str) -> None:
    from .capacity import validate_scope, valid_scopes_for_provider

    try:
        validate_scope(provider, scope)
    except ValueError as exc:
        err(str(exc))
        raise SystemExit(2) from exc
    allowed = valid_scopes_for_provider(provider)
    if scope not in allowed:
        if provider == "copilot":
            err(f"--scope {scope} is not valid for copilot (use one of: {', '.join(sorted(allowed))})")
        elif provider == "kilo":
            err(f"--scope {scope} is not valid for kilo (use one of: {', '.join(sorted(allowed))})")
        elif provider == "minimax":
            err(f"--scope {scope} is not valid for minimax (use one of: {', '.join(sorted(allowed))})")
        else:
            err(f"--scope {scope} is not valid for {provider} (use one of: {', '.join(sorted(allowed))})")
        raise SystemExit(2)


def validate_gate_args(cwd: str, min_remaining: str, poll_interval: str, max_unavailable_wait: str, retry_delays: str) -> None:
    if not Path(cwd).is_dir():
        err(f"--cwd is not a directory: {cwd}")
        raise SystemExit(2)
    if not is_number(min_remaining):
        err("--min-remaining must be numeric")
        raise SystemExit(2)
    if not is_integer(poll_interval):
        err("--poll-interval must be integer seconds")
        raise SystemExit(2)
    if int(poll_interval) < 1:
        err("--poll-interval must be at least 1")
        raise SystemExit(2)
    if not is_integer(max_unavailable_wait):
        err("--max-unavailable-wait must be integer seconds (0 to wait forever)")
        raise SystemExit(2)
    validate_retry_delays(retry_delays)


@dataclass
class RunLogs:
    run_dir: Path
    text_log: Path
    event_log: Path
    prompt_sha: str = ""


def setup_run_logs(log_dir: Path, suffix: str, provider_link: str = "", run_dir: Path | None = None) -> RunLogs:
    old_umask = os.umask(0o077)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            log_dir.chmod(0o700)
        except OSError:
            pass
        if run_dir is None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            import tempfile

            run = Path(tempfile.mkdtemp(prefix=f"{stamp}-{suffix}-", dir=str(log_dir)))
        else:
            run = run_dir
            run.mkdir(parents=True, exist_ok=True)
        try:
            run.chmod(0o700)
        except OSError:
            pass
        text_log = run / "run.log"
        event_log = run / "events.jsonl"
        text_log.touch(exist_ok=True)
        event_log.touch(exist_ok=True)
        for p in (text_log, event_log):
            try:
                p.chmod(0o600)
            except OSError:
                pass
        _symlink(run, log_dir / "latest")
        if provider_link:
            _symlink(run, log_dir / f"latest-{provider_link}")
        return RunLogs(run, text_log, event_log)
    finally:
        os.umask(old_umask)


def _symlink(target: Path, link: Path) -> None:
    try:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    except OSError:
        pass


def log_text(logs: RunLogs, message: str) -> None:
    with logs.text_log.open("a", encoding="utf-8") as fh:
        fh.write(f"[{datetime.now(timezone.utc).astimezone().isoformat()}] {message}\n")


def log_event(logs: RunLogs, event_type: str, data: dict[str, Any] | None = None) -> None:
    obj = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(),
        "type": event_type,
        "data": data or {},
    }
    with logs.event_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")


def load_prompt(prompt_text: str, prompt_file: str, logs: RunLogs) -> tuple[str, str]:
    dest = logs.run_dir / "prompt.txt"
    if prompt_file:
        src = Path(prompt_file)
        try:
            same = src.resolve() == dest.resolve()
        except OSError:
            same = False
        if not same:
            shutil.copyfile(src, dest)
        text = dest.read_text(encoding="utf-8", errors="replace")
    else:
        text = prompt_text
        dest.write_text(text, encoding="utf-8")
    try:
        dest.chmod(0o600)
    except OSError:
        pass
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    logs.prompt_sha = digest
    return text, digest


# Providers recognised as top-level keys of a per-provider
# ``LLM_SCHEDULER_USAGE_JSON`` map. Mirrors the set accepted by
# ``--provider`` validation in the scheduler/ralph-robin CLIs.
KNOWN_PROVIDERS = frozenset({"codex", "claude", "copilot", "kilo", "opencode", "minimax"})


def usage_snapshot_for_provider(provider: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a JSON-friendly snapshot for ``provider``.

    The legacy wire format (e.g. ``five_hour``, ``week``, ``monthly``) is
    preserved for Codex/Claude/Copilot so existing tests, JSON consumers, and
    the ``--prefix usage`` renderer keep working. Kilo is represented through
    ``scopes`` (its new generic form); the legacy keys are absent because
    Kilo has no session windows.
    """
    env = env or os.environ
    injected = env.get("LLM_SCHEDULER_USAGE_JSON")
    if injected:
        raw = json.loads(injected)
        if isinstance(raw, dict) and provider in raw:
            return raw[provider]
        # ``LLM_SCHEDULER_USAGE_JSON`` comes in two shapes: a per-provider map
        # keyed by provider name (e.g. ``{"claude": {...}, "codex": {...}}``)
        # used by ralph-robin's multi-provider tests, or a single flat snapshot
        # (e.g. ``{"available": true, "five_hour": {...}}``) that applies to
        # whichever provider is requested. Only when ``raw`` is a per-provider
        # map that omits this provider do we report "no data": returning the
        # whole map there would feed another provider's data into the decision
        # logic and can produce a bogus future ``wait_until`` that hangs callers
        # in a sleep loop. A flat snapshot must still be returned as-is.
        if isinstance(raw, dict) and any(key in KNOWN_PROVIDERS for key in raw):
            return {
                "provider": provider,
                "available": False,
                "reason": "no-data",
                "scopes": [],
            }
        return raw
    if provider == "codex":
        return json_for_provider(read_codex(env), "codex")
    if provider == "claude":
        return json_for_provider(read_claude(env), "claude")
    if provider == "copilot":
        return json_for_copilot(read_copilot(env), False)
    if provider == "kilo":
        snap = _kilo_snapshot(env)
        return {
            "provider": snap.provider,
            "available": snap.available,
            "reason": snap.reason,
            "source": snap.source,
            "selected_model": snap.selected_model,
            "scopes": [_scope_to_dict(s) for s in snap.scopes],
        }
    if provider == "opencode":
        snap = _opencode_snapshot(env)
        return {
            "provider": snap.provider,
            "available": snap.available,
            "reason": snap.reason,
            "source": snap.source,
            "selected_model": snap.selected_model,
            "scopes": [_scope_to_dict(s) for s in snap.scopes],
        }
    if provider == "minimax":
        snap = _minimax_snapshot(env)
        return {
            "provider": snap.provider,
            "available": snap.available,
            "reason": snap.reason,
            "source": snap.source,
            "selected_model": snap.selected_model,
            "scopes": [_scope_to_dict(s) for s in snap.scopes],
        }
    return {"provider": provider, "available": False, "reason": "unsupported-provider"}


def _kilo_snapshot(env: dict[str, str] | None):
    from .providers import read_kilo

    return read_kilo(env)


def _opencode_snapshot(env: dict[str, str] | None):
    from .providers import read_opencode

    return read_opencode(env)


def _minimax_snapshot(env: dict[str, str] | None):
    from .providers import read_minimax

    return read_minimax(env)


def _scope_to_dict(scope: Any) -> dict[str, Any]:
    if not hasattr(scope, "name"):
        return scope if isinstance(scope, dict) else {}
    return {
        "name": scope.name,
        "kind": scope.kind,
        "ready": scope.ready,
        "reason": scope.reason,
        "remaining_percent": scope.remaining_percent,
        "reset_epoch": scope.reset_epoch,
        "resets_at": scope.resets_at,
        "remaining_amount": scope.remaining_amount,
        "total_amount": scope.total_amount,
        "currency": scope.currency,
        "label": scope.label,
        "source": scope.source,
    }


def _legacy_snapshot_to_scopes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate a legacy ``five_hour``/``week``/``monthly`` snapshot into
    the generic scope dictionaries that :func:`capacity.decide` consumes.

    Lives in ``common`` rather than ``capacity`` to keep the new abstraction
    free of provider-specific keys; this function is the only place the
    translation happens.
    """
    scopes: list[dict[str, Any]] = []
    for name, key in (("5h", "five_hour"), ("weekly", "week")):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        reset = window.get("resets_at")
        reset_epoch = parse_epoch(reset)
        rem = num(window.get("remaining"))
        scopes.append(
            {
                "name": name,
                "kind": "reset_window",
                "ready": rem is not None and rem > 0,
                "reason": "",
                "remaining_percent": rem,
                "reset_epoch": reset_epoch,
                "resets_at": reset,
                "source": snapshot.get("source", ""),
            }
        )
    monthly = snapshot.get("monthly")
    if isinstance(monthly, dict):
        rem = num(monthly.get("remaining"))
        reset_epoch = copilot_monthly_reset_epoch()
        scopes.append(
            {
                "name": "monthly",
                "kind": "reset_window",
                "ready": rem is not None and rem > 0,
                "reason": "",
                "remaining_percent": rem,
                "reset_epoch": reset_epoch,
                "resets_at": str(reset_epoch) if reset_epoch is not None else None,
                "source": snapshot.get("source", ""),
            }
        )
    return scopes


def _decision_scopes(snapshot: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    """Return the decision-ready scope dicts for ``provider``."""
    if provider in ("kilo", "opencode", "minimax"):
        existing = snapshot.get("scopes")
        if isinstance(existing, list) and existing and isinstance(existing[0], dict) and "kind" in existing[0]:
            return existing
    return _legacy_snapshot_to_scopes(snapshot)


def _scope_filtered(scopes: list[dict[str, Any]], requested: str) -> list[dict[str, Any]]:
    if requested == "auto":
        return scopes
    return [s for s in scopes if s.get("name") == requested]


def decide_with_scopes(
    provider: str,
    scope: str,
    min_remaining_percent: float,
    min_remaining_amount: float,
    poll_interval: int,
    scopes: list[dict[str, Any]],
    *,
    cli_present: bool = True,
    snapshot_available: bool = True,
    snapshot_reason: str = "",
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Decide for ``provider`` against the given pre-built scope dicts.

    Thin compatibility shim over :func:`llm_tools.capacity.decide` that keeps
    the existing public JSON shape (``provider``/``usable``/``reason``/
    ``wait_until``/``windows``/``exhausted``) so every consumer keeps
    working unchanged.
    """
    from .capacity import decide, CapacityScope, ProviderSnapshot, SCOPE_AUTO

    now = now_epoch(env)
    poll = max(1, int(poll_interval))

    if not snapshot_available and not scopes:
        return {
            "provider": provider,
            "usable": False,
            "reason": snapshot_reason or "unavailable",
            "wait_until": now + poll,
            "windows": [],
        }
    if not cli_present:
        return {
            "provider": provider,
            "usable": False,
            "reason": "missing-cli",
            "wait_until": now + poll,
            "windows": _windows_from_dicts(scopes),
        }

    if scope == SCOPE_AUTO:
        chosen = list(scopes)
    else:
        chosen = [s for s in scopes if s.get("name") == scope]

    typed_scopes = [_dict_to_scope(s) for s in chosen]
    snap = ProviderSnapshot(
        provider=provider,
        available=bool(snapshot_available),
        reason=str(snapshot_reason),
        scopes=typed_scopes,
    )
    decision = decide(
        snap,
        scope,
        min_remaining_percent,
        min_remaining_amount,
        poll,
        cli_present=cli_present,
        env=env,
    )
    return _decision_to_legacy(decision, scopes, provider)


def _dict_to_scope(d: dict[str, Any]) -> Any:
    from .capacity import CapacityScope

    return CapacityScope(
        name=str(d.get("name", "")),
        kind=str(d.get("kind", "reset_window")),
        ready=bool(d.get("ready", True)),
        reason=str(d.get("reason", "")),
        remaining_percent=d.get("remaining_percent"),
        reset_epoch=d.get("reset_epoch"),
        resets_at=d.get("resets_at"),
        remaining_amount=d.get("remaining_amount"),
        total_amount=d.get("total_amount"),
        currency=d.get("currency"),
        label=d.get("label"),
        source=str(d.get("source", "")),
    )


def _windows_from_dicts(scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": s.get("name"),
            "kind": s.get("kind"),
            "remaining": s.get("remaining_percent"),
            "remaining_amount": s.get("remaining_amount"),
            "currency": s.get("currency"),
            "resets_at": s.get("resets_at"),
            "reset_epoch": s.get("reset_epoch"),
            "source": s.get("source", ""),
        }
        for s in scopes
    ]


def _decision_to_legacy(decision: Any, original_scopes: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "provider": provider,
        "usable": decision.usable,
        "reason": decision.reason,
        "wait_until": decision.wait_until,
        "windows": _windows_from_dicts(original_scopes),
    }
    if decision.exhausted:
        out["exhausted"] = [
            {
                "name": s.name,
                "kind": s.kind,
                "remaining": s.remaining_percent,
                "remaining_amount": s.remaining_amount,
                "reset_epoch": s.reset_epoch,
            }
            for s in decision.exhausted
        ]
    return out


def _window_scope_dict(name: str, window: dict[str, Any], source: str) -> dict[str, Any]:
    """Build a decision-ready scope dict from a normalized window."""
    rem = num(window.get("remaining"))
    if rem is None:
        rem = remaining_from_used(window.get("used"))
    reset = window.get("resets_at")
    return {
        "name": name,
        "kind": "reset_window",
        "ready": rem is not None and rem > 0,
        "reason": "",
        "remaining_percent": rem,
        "reset_epoch": parse_epoch(reset),
        "resets_at": reset,
        "source": source,
    }


def model_decision_scopes(provider: str, snapshot: dict[str, Any], model: str | None) -> list[dict[str, Any]] | None:
    """Decision scopes for a provider's *specific* model, or ``None``.

    Returns ``None`` when the provider/model combination has no dedicated
    rate-limit bucket (most providers/models) so callers fall back to the
    aggregate window. Today only Claude (per-model weekly buckets) and Codex
    (the ``codex-spark`` row) expose model-specific limits; the mechanism is
    generic so new ones slot in here.
    """
    if not model:
        return None
    want = model.strip().lower()
    if provider == "claude":
        entries = snapshot.get("model_weeks")
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("model") or "").strip().lower()
            # Match a short alias ("sonnet") or a full id ("claude-sonnet-4-6")
            # against the bucket label ("Sonnet") in either direction.
            if label and (label == want or label in want or want in label):
                week = entry.get("week")
                if not isinstance(week, dict):
                    return None
                return [_window_scope_dict("weekly", week, str(snapshot.get("source", "")))]
        return None
    if provider == "codex":
        # The spark row is the only per-model Codex bucket; any other model maps
        # to the aggregate "codex" row, which is already the default gate.
        if "spark" not in want:
            return None
        rows = snapshot.get("rows")
        if not isinstance(rows, list):
            return None
        spark = next((r for r in rows if isinstance(r, dict) and r.get("key") == "codex-spark"), None)
        if spark is None:
            return None
        source = str(spark.get("source") or snapshot.get("source", ""))
        scopes: list[dict[str, Any]] = []
        for name, key in (("5h", "five_hour"), ("weekly", "week")):
            window = spark.get(key)
            if isinstance(window, dict):
                scopes.append(_window_scope_dict(name, window, source))
        return scopes or None
    return None


def usage_decision_for_provider(
    provider: str,
    window: str,
    min_remaining: str,
    poll_interval: str,
    snapshot: dict[str, Any],
    env: dict[str, str] | None = None,
    *,
    model: str | None = None,
    allow_fallback: bool = True,
) -> dict[str, Any]:
    """Decide whether ``provider`` is usable under the requested ``window``.

    Implementation is a thin compatibility shim over
    :func:`llm_tools.capacity.decide`: it translates the legacy
    ``window``/``snapshot`` shape into generic scope dicts, calls the
    generic decider, and re-shapes the result to keep the existing public
    JSON (``windows``, ``usable``, ``reason``, ``wait_until``,
    ``exhausted``) stable for every consumer (scheduler, ralph, tests).

    When ``model`` names a provider model that has its own rate-limit bucket
    (e.g. Claude Sonnet, Codex Spark) the decision becomes model-aware:

    * ``allow_fallback=False`` (the default for pinned models) gates *only* on
      that model's limit, so an exhausted model makes the provider unusable and
      callers rotate away instead of silently running a different model.
    * ``allow_fallback=True`` keeps the aggregate gate (the provider stays
      usable while any model has room) but reports ``model_exhausted`` so the
      caller can drop the model pin and let the CLI choose.

    The returned dict carries ``model`` and ``model_exhausted`` whenever a model
    was supplied.
    """
    env = env or os.environ
    poll = max(1, int(poll_interval))
    min_percent = float(min_remaining)
    min_amount = float(min_remaining)
    try:
        from .providers import kilo_min_balance, opencode_min_balance

        if provider == "kilo":
            min_amount = kilo_min_balance(env)
        elif provider == "opencode":
            min_amount = opencode_min_balance(env)
    except Exception:
        pass

    available = bool(snapshot.get("available", True))
    reason = str(snapshot.get("reason", ""))

    def decide_on(base_scopes: list[dict[str, Any]]) -> dict[str, Any]:
        return decide_with_scopes(
            provider,
            window,
            min_percent,
            min_amount,
            poll,
            _scope_filtered(base_scopes, window),
            cli_present=True,
            snapshot_available=available,
            snapshot_reason=reason,
            env=env,
        )

    model_scopes = model_decision_scopes(provider, snapshot, model)
    # A non-auto scope the model has no bucket for cannot gate on the model.
    if model_scopes is not None and window != "auto" and not any(s.get("name") == window for s in model_scopes):
        model_scopes = None
    model_decision = decide_on(model_scopes) if model_scopes is not None else None

    if model_scopes is not None and not allow_fallback:
        decision = model_decision
    else:
        decision = decide_on(_decision_scopes(snapshot, provider))

    if model:
        decision["model"] = model
        decision["model_exhausted"] = (
            model_decision.get("usable") is not True if model_decision is not None else False
        )
    return decision


def usage_snapshot_and_decision(
    provider: str,
    capacity_provider: str | None,
    window: str,
    min_remaining: str,
    poll_interval: str,
    env: dict[str, Any] | None = None,
    *,
    model: str | None = None,
    allow_fallback: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Snapshot + usability decision for ``provider``, optionally delegated.

    When ``capacity_provider`` is set and differs from ``provider``, the snapshot
    and decision are read from that other provider's usage windows instead of
    ``provider``'s own — the generic "this CLI actually runs another provider's
    model" case (e.g. OpenCode pointed at MiniMax). The decision is gated purely
    on the capacity provider's aggregate windows (the requesting provider's
    pinned ``model`` has no rate-limit bucket there), then relabelled back to
    ``provider`` and tagged with ``capacity_provider`` so callers, logs, and the
    rotation display still reason about the provider whose CLI actually runs.

    Returns ``(snapshot, decision)``. With no delegation it is exactly the
    ``usage_snapshot_for_provider`` + ``usage_decision_for_provider`` pair.
    """
    source = capacity_provider or provider
    snapshot = usage_snapshot_for_provider(source, env)
    if source == provider:
        decision = usage_decision_for_provider(
            provider, window, min_remaining, poll_interval, snapshot, env,
            model=model, allow_fallback=allow_fallback,
        )
        return snapshot, decision
    decision = dict(
        usage_decision_for_provider(
            source, window, min_remaining, poll_interval, snapshot, env,
            model=None, allow_fallback=True,
        )
    )
    decision["provider"] = provider
    decision["capacity_provider"] = source
    return snapshot, decision


def argv_to_command_line(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def template_argv(template: str, *, provider: str, prompt: str, prompt_file: Path, cwd: str) -> list[str]:
    parts = shlex.split(template)
    values = {"{provider}": provider, "{prompt}": prompt, "{prompt_file}": str(prompt_file), "{cwd}": cwd}
    out = []
    for part in parts:
        for key, value in values.items():
            part = part.replace(key, value)
        out.append(part)
    return out


# Prompts ralph-robin/llm-scheduler may safely auto-acknowledge.
SAFE_TRUST_PROMPTS = ("Confirm folder trust", "Do you trust the files in this folder?")

# Interactive prompts that mean the CLI has stopped to wait for a human decision
# (e.g. it ran out of credit and is offering to wait/upgrade). Matching any of
# these is treated as a no-progress block and the run is aborted + terminated.
BLOCKING_PROMPT_PATTERNS = (
    re.compile(r"\bwhat do you want to do\?", re.I),
    re.compile(r"\benter to confirm\b", re.I),
    re.compile(r"\besc to cancel\b", re.I),
    re.compile(r"\buse (?:the )?arrow keys\b", re.I),
    re.compile(r"\bpress (?:enter|return) to\b", re.I),
    re.compile(r"\badjust monthly spend limit\b", re.I),
    re.compile(r"\bwait for limit to reset\b", re.I),
    re.compile(r"\bupgrade to max\b", re.I),
    re.compile(r"\byou(?:'|’)ve hit your monthly spend limit\b", re.I),
    re.compile(r"\bmonthly spend limit\b", re.I),
    re.compile(r"\brate[- ]limit options\b", re.I),
    re.compile(r"\b(?:run|reach|reached|hit)[\w ]{0,30}\busage limit\b", re.I),
    re.compile(r"\bout of (?:credit|credits|tokens)\b", re.I),
    re.compile(r"\bbuy more (?:credits|tokens)\b", re.I),
    re.compile(r"\bdowngrade to (?:a )?(?:simpler|smaller|cheaper) model\b", re.I),
    re.compile(r"\b(?:proceed|continue)\?\s*(?:\[[yYnN]/[yYnN]\]|\([yYnN]/[yYnN]\))", re.I),
)

# A line that reads like the CLI asked the user a question and is now waiting.
QUESTION_LINE_RE = re.compile(
    r"(?im)^\s*(?:[>\-\*\d.)\s]*)?(?:what|which|who|when|where|why|how|do you|would you|should i|should we|can i|may i|please (?:choose|select)|select|choose|confirm|proceed|continue)\b[^\n?]{0,180}\?\s*$"
)


def run_pty_capture(
    argv: Sequence[str],
    cwd: Path,
    timeout: int,
    *,
    stream: bool,
    auto_confirm: bool,
    detect_prompts: bool = True,
    output_path: Path | None = None,
    status_path: Path | None = None,
    idle_timeout: int = 0,
    question_idle_timeout: int = 0,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    safe_prompts = SAFE_TRUST_PROMPTS
    blocking_patterns = BLOCKING_PROMPT_PATTERNS
    question_line = QUESTION_LINE_RE
    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
        except OSError:
            pass
        if env is None:
            os.execvp(argv[0], list(argv))
        os.execvpe(argv[0], list(argv), env)
    try:
        if sys.stdout.isatty():
            winsize = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        else:
            winsize = struct.pack("HHHH", 30, 240, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass

    chunks: list[str] = []
    trust_sent = False
    exit_code = 124
    start = time.time()
    last_progress = start
    question_seen_at: float | None = None
    abort_reason = ""

    def reap(status: int) -> int | None:
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return 128 + os.WTERMSIG(status)
        return None

    def record_abort(reason: str) -> None:
        nonlocal abort_reason, exit_code
        abort_reason = reason
        exit_code = AUTONOMY_ABORT_STATUS
        line = f"\nllm-scheduler: autonomous abort: {reason}\n"
        chunks.append(line)
        if stream:
            os.write(sys.stdout.fileno(), line.encode("utf-8", "replace"))

    eof = False
    try:
        while True:
            now = time.time()
            if now - start > timeout:
                break
            if idle_timeout > 0 and now - last_progress > idle_timeout:
                record_abort(f"no output progress for {idle_timeout}s")
                break
            if question_idle_timeout > 0 and question_seen_at is not None and now - question_seen_at > question_idle_timeout:
                record_abort(f"question required a response for {question_idle_timeout}s")
                break
            ready, _, _ = select.select([fd], [], [], 0.2)
            if fd in ready:
                try:
                    raw = os.read(fd, 65536)
                except OSError:
                    eof = True
                    break
                if not raw:
                    eof = True
                    break
                text = raw.decode("utf-8", "replace")
                chunks.append(text)
                last_progress = time.time()
                if stream:
                    try:
                        os.write(sys.stdout.fileno(), raw)
                    except OSError:
                        stream = False
                combined = strip_ansi("".join(chunks))
                if auto_confirm and not trust_sent and any(p in combined for p in safe_prompts):
                    os.write(fd, b"\r")
                    trust_sent = True
                    question_seen_at = None
                elif detect_prompts and any(pattern.search(combined) for pattern in blocking_patterns):
                    record_abort("interactive prompt detected")
                    break
                elif detect_prompts and question_line.search(combined[-4000:]):
                    question_seen_at = time.time()
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done, status = pid, 0
            if done == pid:
                code = reap(status)
                if code is not None:
                    exit_code = code
                break
    except KeyboardInterrupt:
        exit_code = 130
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(0.2)
            try:
                done, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if done == pid:
                break
    if exit_code == 124:
        deadline = time.time() + (5.0 if eof else 0.0)
        while True:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if done == pid:
                code = reap(status)
                if code is not None:
                    exit_code = code
                break
            if time.time() >= deadline:
                break
            time.sleep(0.01)
    if exit_code in (124, AUTONOMY_ABORT_STATUS):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            time.sleep(0.2)
    text = strip_ansi("".join(chunks))
    if output_path:
        output_path.write_text(text, encoding="utf-8")
    if status_path:
        status_path.write_text(str(exit_code), encoding="utf-8")
    return exit_code, text


def clean_capture_file(path: Path) -> None:
    if path.is_file():
        path.write_text(strip_ansi(path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


# Provider/transport rate-limit signatures. These must be specific enough that a
# model's own prose never trips them: an autonomous prompt that talks about a
# device being "overloaded", a service being "temporarily unavailable", or
# advising to "try again later" is describing the SYSTEM UNDER TEST, not the LLM
# provider. Matching those bare words misread a successful provider hand-off as a
# failed submission and re-ran or killed the orchestration loop. Each branch here
# is anchored to a genuine API/HTTP/CLI rate-limit shape.
PROVIDER_RATE_LIMIT_RE = re.compile(
    r"rate[ _-]?limit(?:ed)? (?:exceeded|reached|hit|error)"
    r"|rate_limit_error"
    r"|too many requests"
    r"|http[ /?]429|status[ :]?429|429 too many requests"
    r"|quota (?:exceeded|reached)"
    r"|usage limit (?:exceeded|reached)"
    r"|overloaded_error|\"overloaded\"|api error:? ?overloaded"
    r"|http 503|503 service unavailable|service unavailable \(503\)",
    re.I,
)


def output_is_retryable(status: int, output: str, attached: bool = False, trust_clean_exit: bool = False) -> bool:
    if attached:
        return status not in (0, 130, 143)
    if status != 0:
        return True
    # Under an orchestrator that owns rate-limit handling (ralph-robin gates on
    # usage data and rotates/suspends between increments), a clean provider exit
    # is a completed increment and must be trusted. Scanning the model's own
    # output for rate-limit-ish words then double-counts the SYSTEM UNDER TEST's
    # prose (e.g. "the device was overloaded") as a provider failure, which is
    # what previously re-ran the same work and finally killed the loop.
    if trust_clean_exit:
        return False
    return bool(PROVIDER_RATE_LIMIT_RE.search(output))


# Fields that may appear, in any combination and order, inside the `[ ]` marker
# prepended to each relayed provider line. "time" is the wall-clock HH:MM:SS;
# "provider" is the provider name (codex/claude/...), "usage" is the remaining
# percentage per window (e.g. "5h=10% week=30%"). An empty selection disables
# the marker entirely (no brackets).
LINE_PREFIX_FIELDS = ("time", "provider", "usage")

# Short labels for the scopes shown by the "usage" prefix field.
USAGE_PREFIX_WINDOW_LABELS = {
    "weekly": "week",
    "monthly": "month",
    "balance": "bal",
    "budget": "bud",
    "ungated": "free",
    "byok": "byok",
    "local": "local",
}


def usage_prefix_text(provider: str, env: dict[str, str] | None = None) -> str:
    """Remaining-percentage summary for the "usage" prefix field.

    Renders e.g. ``5h=10% week=30%`` for the provider's current scopes, or
    ``bal=£12.40`` / ``bud=62%`` for Kilo. Returns an empty string when no
    scope has a usable remaining value. This shells out to the provider
    usage source, so callers must cache it (see UsagePrefixCache) instead
    of calling it per line.
    """
    snapshot = usage_snapshot_for_provider(provider, env)
    decision = usage_decision_for_provider(provider, "auto", "1", "60", snapshot, env)
    windows = decision.get("windows")
    if not isinstance(windows, list):
        return ""
    parts: list[str] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        name = str(window.get("name", "?"))
        label = USAGE_PREFIX_WINDOW_LABELS.get(name, name)
        kind = window.get("kind") or "reset_window"
        if kind == "balance":
            amount = window.get("remaining_amount")
            if amount is None:
                continue
            currency = window.get("currency") or ""
            if currency:
                parts.append(f"{label}={currency}{fmt_number(amount)}")
            else:
                parts.append(f"{label}={fmt_number(amount)}")
            continue
        if kind == "ungated":
            text = window.get("label") or name
            parts.append(f"{label}={text}")
            continue
        remaining = window.get("remaining")
        if remaining is None:
            continue
        parts.append(f"{label}={fmt_pct(remaining)}%")
    return " ".join(parts)


class UsagePrefixCache:
    """Process-local TTL cache for the per-provider "usage" prefix string.

    Computing the usage field shells out to provider CLIs, far too slow to do per
    relayed line. This caches the rendered string per provider and recomputes it at
    most once per ``ttl`` seconds, so the field stays cheap enough to stamp on
    every line. One module-level instance (USAGE_PREFIX_CACHE) is shared across
    the stdout/stderr prefixers and successive ralph-robin increments in the same
    process, so the refresh cadence holds across the whole run.
    """

    def __init__(self, clock: Any | None = None, builder: Any | None = None) -> None:
        self._clock = clock or time.monotonic
        self._builder = builder or usage_prefix_text
        self._cache: dict[str, tuple[float, str]] = {}

    def get(self, provider: str, ttl: float = 15.0) -> str:
        now = self._clock()
        hit = self._cache.get(provider)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        try:
            value = self._builder(provider)
        except Exception:
            # A transient usage-source failure must not break output relaying:
            # reuse the last known value (or empty) and try again next interval.
            value = hit[1] if hit is not None else ""
        self._cache[provider] = (now, value)
        return value


USAGE_PREFIX_CACHE = UsagePrefixCache()


def render_line_prefix(
    fields: list[str],
    provider: str = "",
    now: float | None = None,
    usage_ttl: float = 15.0,
    usage_cache: UsagePrefixCache | None = None,
) -> bytes:
    """Render the `[...] ` marker prepended to a relayed provider line.

    ``fields`` is an ordered subset of LINE_PREFIX_FIELDS; the order is the order
    rendered inside the brackets. Returns empty bytes when nothing resolves to
    content, so a disabled/empty selection emits no marker at all (not even the
    brackets).
    """
    parts: list[str] = []
    for name in fields:
        if name == "time":
            moment = time.localtime(now if now is not None else time.time())
            parts.append(time.strftime("%H:%M:%S", moment))
        elif name == "provider" and provider:
            parts.append(provider)
        elif name == "usage" and provider:
            cache = usage_cache if usage_cache is not None else USAGE_PREFIX_CACHE
            text = cache.get(provider, usage_ttl)
            if text:
                parts.append(text)
    if not parts:
        return b""
    return ("[" + " ".join(parts) + "] ").encode("utf-8")


class LinePrefixer:
    """Prefix each line of streamed provider output with a configurable marker.

    A long autonomous increment can go minutes between visible lines; a per-line
    marker (wall-clock time, the provider name, and/or remaining usage) lets a
    watcher tell "thinking" from "wedged" and tell which provider in the rotation is
    talking. This stamps the STREAMED copy only — the captured transcript that is
    logged and scanned for rate-limit signatures stays byte-exact. It tracks
    line-start state across calls so chunked, non-line-aligned output (a PTY that
    emits half a line at a time) is still stamped exactly once per line. An empty
    ``fields`` disables prefixing entirely.
    """

    def __init__(
        self,
        fields: list[str] | None = None,
        provider: str = "",
        clock: Any | None = None,
        usage_ttl: float = 15.0,
        usage_cache: UsagePrefixCache | None = None,
    ) -> None:
        self.fields = list(fields or [])
        self.provider = provider
        self.at_line_start = True
        self._clock = clock or time.time
        self._usage_ttl = usage_ttl
        self._usage_cache = usage_cache

    @property
    def enabled(self) -> bool:
        return bool(self.fields)

    def apply(self, raw: bytes) -> bytes:
        if not self.enabled or not raw:
            return raw
        stamp = render_line_prefix(self.fields, self.provider, self._clock(), self._usage_ttl, self._usage_cache)
        if not stamp:
            return raw
        out = bytearray()
        i = 0
        n = len(raw)
        while i < n:
            if self.at_line_start:
                out += stamp
                self.at_line_start = False
            nl = raw.find(b"\n", i)
            if nl == -1:
                out += raw[i:]
                break
            out += raw[i : nl + 1]
            i = nl + 1
            self.at_line_start = True
        return bytes(out)


def wake_diagnostics() -> dict[str, Any]:
    user_systemd = "unknown"
    if have_cmd("systemctl"):
        proc = subprocess.run(["systemctl", "--user", "is-system-running"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        state = proc.stdout.strip()
        user_systemd = state or "unknown"
    return {
        "backend": power_backend(),
        "systemd_run": have_cmd("systemd-run"),
        "systemd_inhibit": have_cmd("systemd-inhibit"),
        "rtcwake": have_cmd("rtcwake"),
        "rtc_wakealarm": str(rtc_wakealarm_path()),
        "rtc_wakealarm_readable": read_rtc_wakealarm() is not None or rtc_wakealarm_path().exists(),
        "watchdog_device": watchdog_device_path() if watchdog_available() else None,
        "user_systemd": user_systemd,
        "note": "wake is best effort and depends on firmware, kernel, RTC, and systemd support",
    }


# ============================================================================
# Suspend / wake reliability (portable, feature-detected seam)
# ============================================================================
#
# Power management is OS-specific, so everything here is written against a small
# capability-detected backend. The *policy* (idle inhibition, RTC-armed suspend,
# wake verification by drift, the durable cycle ledger, the watchdog safety net)
# is platform-agnostic; only the handful of `_systemd_*` primitives shell out to
# Linux tools. A future macOS backend (`caffeinate` / `pmset schedule wake`) can
# be added by implementing the same primitives without touching any caller.
#
# On a host missing the required tools every helper degrades to "unavailable"
# and the caller falls back to an in-process wall-clock wait: the machine simply
# stays awake. That is always correct -- it only forgoes the power saving.


def power_backend(env: dict[str, str] | None = None) -> str:
    """Name of the active power backend: ``systemd``, or ``none``.

    Detected by capability, never by distro string, so it works across all
    systemd-based Linux. (macOS would report a future ``darwin`` backend.)
    """
    if have_cmd("systemctl") and have_cmd("systemd-run"):
        return "systemd"
    return "none"


def rtc_wakealarm_path(env: dict[str, str] | None = None) -> Path:
    override = (env or os.environ).get("LLM_TOOLS_RTC_WAKEALARM")
    return Path(override) if override else Path("/sys/class/rtc/rtc0/wakealarm")


def read_rtc_wakealarm(env: dict[str, str] | None = None) -> int | None:
    """The RTC wake alarm epoch currently programmed, or None if unset/unreadable.

    systemd programs this from a ``WakeSystem=true`` timer just before the kernel
    enters sleep and clears it on resume, so a non-empty value mid-flight is the
    ground-truth confirmation that a wake is actually armed.
    """
    try:
        raw = rtc_wakealarm_path(env).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def suspend_drift_tolerance(env: dict[str, str] | None = None) -> int:
    """How far from target a wake may land and still count as reliable (seconds)."""
    raw = (env or os.environ).get("LLM_TOOLS_SUSPEND_DRIFT_TOLERANCE", "90") or "90"
    try:
        return max(5, int(float(raw)))
    except ValueError:
        return 90


class IdleSuspendInhibitor:
    """Hold a logind ``idle`` inhibitor for the life of this process.

    Why: a desktop idle timer (e.g. KDE PowerDevil's AC auto-suspend, GNOME, or
    logind ``IdleAction``) will suspend the machine after N minutes of no input,
    *even while a headless job is busy working*, with no RTC wake armed -- the
    box then sleeps until someone touches it. Holding an ``idle`` inhibitor stops
    that automatic action. Crucially an ``idle`` inhibitor does NOT block an
    explicit ``systemctl suspend``, so the orchestrator keeps full control of its
    own deliberate, RTC-armed suspends.

    The lock is tied to a pipe owned by this process: if we exit or crash, the
    pipe closes, the helper sees EOF and exits, and logind drops the lock. No
    leaked inhibitor outlives the run.

    Portability: uses ``systemd-inhibit`` (present on essentially all systemd
    Linux). Absent that, acquisition is a no-op and the caller is told, so it can
    warn the user that auto-suspend may interrupt the run.
    """

    def __init__(self, who: str, why: str, env: dict[str, str] | None = None) -> None:
        self.who = who
        self.why = why
        # Honor an explicitly passed dict (even an empty one) so tests are not
        # silently handed the ambient os.environ.
        self.env = env if env is not None else os.environ
        self._proc: subprocess.Popen[bytes] | None = None
        self._wfd: int | None = None

    def acquire(self) -> bool:
        if self.env.get("LLM_TOOLS_NO_INHIBIT", "0") == "1":
            return False
        if not have_cmd("systemd-inhibit"):
            return False
        try:
            read_fd, write_fd = os.pipe()
        except OSError:
            return False
        try:
            self._proc = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=idle",
                    f"--who={self.who}",
                    f"--why={self.why}",
                    "--mode=block",
                    "cat",
                ],
                stdin=read_fd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            os.close(read_fd)
            os.close(write_fd)
            self._proc = None
            return False
        os.close(read_fd)
        self._wfd = write_fd
        return True

    def release(self) -> None:
        if self._wfd is not None:
            try:
                os.close(self._wfd)
            except OSError:
                pass
            self._wfd = None
        if self._proc is not None:
            try:
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def __enter__(self) -> "IdleSuspendInhibitor":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:
        # Belt and suspenders: drop the lock if the holder is garbage-collected
        # without an explicit release (the pipe also releases it on process exit).
        try:
            self.release()
        except Exception:
            pass


def watchdog_device_path(env: dict[str, str] | None = None) -> str:
    return (env or os.environ).get("LLM_TOOLS_WATCHDOG_DEVICE", "/dev/watchdog")


def watchdog_available(env: dict[str, str] | None = None) -> bool:
    try:
        return Path(watchdog_device_path(env)).exists()
    except OSError:
        return False


# Linux watchdog ioctls (see <linux/watchdog.h>): set the bite timeout and ping.
_WDIOC_KEEPALIVE = 0x80045705
_WDIOC_SETTIMEOUT = 0xC0045706


class Watchdog:
    """Best-effort hardware-watchdog safety net for a wedged resume.

    Opt-in recovery for the worst case: the kernel comes back from suspend but
    hangs in device resume (a classic GPU/USB failure mode), leaving the box on
    but dead. A hardware watchdog that is armed and then *not* petted will reset
    the machine, so an unattended run can recover on the next boot instead of
    hanging until someone walks over.

    Limitations (documented, not hidden): this needs a usable ``/dev/watchdog``,
    and whether the device keeps counting across S3 is driver/firmware specific.
    Many soft watchdogs are stopped on suspend; a TCO/iTCO or IPMI watchdog, or
    ``RuntimeWatchdogSec=`` in ``systemd-system.conf``, is the reliable backing.
    Absent a device this is a logged no-op -- never a hard failure.
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env or os.environ
        self._fd: int | None = None

    def arm(self, timeout_seconds: int) -> bool:
        if self.env.get("LLM_TOOLS_NO_WATCHDOG", "0") == "1":
            return False
        device = watchdog_device_path(self.env)
        try:
            self._fd = os.open(device, os.O_WRONLY)
        except OSError:
            self._fd = None
            return False
        try:
            fcntl.ioctl(self._fd, _WDIOC_SETTIMEOUT, struct.pack("i", int(timeout_seconds)))
        except OSError:
            # Fake/soft devices (and tests) may not support the ioctl; the
            # open succeeded, so treat it as armed and rely on keepalive/close.
            pass
        return True

    def keepalive(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.ioctl(self._fd, _WDIOC_KEEPALIVE, struct.pack("i", 0))
        except OSError:
            try:
                os.write(self._fd, b"\0")
            except OSError:
                pass

    def disarm(self) -> None:
        """Cleanly stop the watchdog: writing the magic 'V' before close tells
        the driver not to bite after we let go of the device."""
        if self._fd is None:
            return
        try:
            os.write(self._fd, b"V")
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


def suspend_ledger_path(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "ralph-robin" / "suspend-ledger.jsonl"


def _append_ledger(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def ledger_record_start(path: Path, cycle_id: str, target_epoch: int, env: dict[str, str] | None = None, **extra: Any) -> None:
    """Durably record (with fsync) that a suspend cycle is starting.

    Written *before* `systemctl suspend` so that if the resume wedges the machine
    and it is hard-reset, the next boot still finds a start with no matching
    ``done`` -- ground truth that a cycle failed to resume.
    """
    record = {"event": "suspend_start", "cycle_id": cycle_id, "target_epoch": int(target_epoch), "started_epoch": now_epoch(env)}
    record.update(extra)
    _append_ledger(path, record)


def ledger_record_done(path: Path, cycle_id: str, woke_epoch: int, reliable: bool, env: dict[str, str] | None = None, **extra: Any) -> None:
    record = {"event": "suspend_done", "cycle_id": cycle_id, "woke_epoch": int(woke_epoch), "reliable": bool(reliable)}
    record.update(extra)
    _append_ledger(path, record)


def incomplete_suspend_cycles(path: Path) -> list[dict[str, Any]]:
    """Suspend cycles with a start but no matching done -- i.e. a resume that
    never completed (a wedged/hard-reset cycle from a prior boot, or one in
    flight). Used on startup to surface a past failed wake to the user."""
    starts: dict[str, dict[str, Any]] = {}
    done: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        obj = read_json_text(line)
        if not isinstance(obj, dict):
            continue
        cid = str(obj.get("cycle_id") or "")
        if not cid:
            continue
        if obj.get("event") == "suspend_start":
            starts[cid] = obj
        elif obj.get("event") == "suspend_done":
            done.add(cid)
    return [rec for cid, rec in starts.items() if cid not in done]


def trim_ledger(path: Path, keep: int = 200) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= keep:
        return
    try:
        path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    except OSError:
        pass


@dataclass
class WakeArm:
    armed: bool
    method: str            # "rtcwake" | "systemd-timer" | "none"
    confirmed: bool        # RTC wakealarm read back at/near target
    unit: str = ""
    detail: str = ""


def arm_rtc_wake(target_epoch: int, who: str, env: dict[str, str] | None = None) -> WakeArm:
    """Arm an RTC alarm to wake the machine at ``target_epoch``.

    Order of preference:
      1. ``rtcwake -m no`` -- programs the alarm without suspending, and the
         result is *verifiable* by reading back the RTC wakealarm. Needs the
         privilege to touch the RTC, so it usually only works as root.
      2. A ``systemd-run --user`` transient timer with ``WakeSystem=true`` -- the
         unprivileged path systemd uses to program the alarm at suspend time.
         Proven to wake real hardware, but not verifiable until suspend.

    ``confirmed`` is True only when the alarm can be read back from sysfs, which
    is the strongest guarantee a wake will fire.
    """
    env = env or os.environ
    now = now_epoch(env)
    tolerance = suspend_drift_tolerance(env)

    if have_cmd("rtcwake"):
        proc = subprocess.run(
            ["rtcwake", "-m", "no", "-t", str(int(target_epoch))],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        if proc.returncode == 0:
            alarm = read_rtc_wakealarm(env)
            confirmed = alarm is not None and abs(alarm - target_epoch) <= tolerance
            if confirmed:
                return WakeArm(True, "rtcwake", True, detail=proc.stdout.strip())

    if power_backend(env) == "systemd":
        unit = f"{who}-wake-{int(time.time())}"
        proc = subprocess.run(
            ["systemd-run", "--user", f"--unit={unit}", f"--on-calendar=@{int(target_epoch)}", "--timer-property=WakeSystem=true", "/bin/true"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        if proc.returncode == 0:
            active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{unit}.timer"], check=False)
            if active.returncode == 0:
                # systemd only programs the RTC at suspend time, so it is not yet
                # confirmable here; the post-resume drift check is the verification.
                _ = now
                return WakeArm(True, "systemd-timer", False, unit=unit, detail=proc.stdout.strip())
        return WakeArm(False, "none", False, detail=proc.stdout.strip())

    return WakeArm(False, "none", False)


def disarm_rtc_wake(arm: WakeArm, env: dict[str, str] | None = None) -> None:
    """Cancel a wake we armed but no longer need (e.g. suspend was skipped)."""
    if arm.method == "systemd-timer" and arm.unit and have_cmd("systemctl"):
        subprocess.run(["systemctl", "--user", "stop", f"{arm.unit}.timer"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    elif arm.method == "rtcwake" and have_cmd("rtcwake"):
        subprocess.run(["rtcwake", "-m", "disable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


@dataclass
class SuspendOutcome:
    suspended: bool          # the machine was (or would have been) put to sleep
    woke_epoch: int | None   # wall clock observed after resume
    target_epoch: int
    drift_seconds: int | None
    reliable: bool           # woke within tolerance of target (wake verified by behaviour)
    method: str              # how the wake was armed
    reason: str              # "ok" | "missing-backend" | "insufficient-lead" | "arm-failed" | "simulated"


def wall_clock_wait_until(target_epoch: int, env: dict[str, str] | None = None) -> None:
    """Wait until wall-clock time reaches ``target_epoch``, in bounded chunks.

    ``time.sleep`` counts CLOCK_MONOTONIC, which the kernel freezes across
    suspend, so a single long sleep would keep counting *awake* seconds after a
    resume and overshoot. Re-checking the wall clock between short chunks ends
    the wait as soon as real time catches up -- right at the RTC wake -- no
    matter how long the machine was actually asleep.
    """
    raw = (env or os.environ).get("LLM_TOOLS_WAIT_POLL_SECONDS", "30") or "30"
    try:
        chunk = max(1.0, float(raw))
    except ValueError:
        chunk = 30.0
    while True:
        remaining = target_epoch - now_epoch(env)
        if remaining <= 0:
            return
        time.sleep(min(float(remaining), chunk))


def suspend_with_wake(
    target_epoch: int,
    *,
    who: str,
    logs: "RunLogs | None" = None,
    min_lead: int | None = None,
    watchdog: bool = False,
    cycle_id: str | None = None,
    env: dict[str, str] | None = None,
) -> SuspendOutcome:
    """Suspend the machine now and reliably wake it at ``target_epoch``.

    The full hardened cycle, shared by ralph-robin and the soak test:

      arm + verify RTC wake -> durable ledger start -> (optional watchdog) ->
      sync -> systemctl suspend -> wall-clock wait to target -> measure drift ->
      ledger done.

    Returns a :class:`SuspendOutcome`. When the backend is unavailable, the lead
    is too short, or arming fails, ``suspended`` is False and the caller falls
    back to an in-process wait (machine stays awake). ``LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1``
    simulates a perfect cycle so the orchestration is testable without sleeping
    the host.
    """
    env = env or os.environ
    now = now_epoch(env)
    cycle_id = cycle_id or f"{who}-{int(time.time() * 1000)}-{os.getpid()}"
    ledger = suspend_ledger_path(env)
    lead = int(target_epoch) - now
    floor = min_lead if min_lead is not None else int(env.get("LLM_SCHEDULER_SUSPEND_MIN_LEAD", "120") or "120")

    def _log(event: str, data: dict[str, Any]) -> None:
        if logs is not None:
            log_event(logs, event, data)

    if env.get("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "0") == "1":
        _log("suspend_simulated", {"cycle_id": cycle_id, "target_epoch": int(target_epoch)})
        return SuspendOutcome(False, target_epoch, int(target_epoch), 0, True, "simulated", "simulated")

    if power_backend(env) == "none":
        _log("suspend_skipped", {"cycle_id": cycle_id, "reason": "missing-backend"})
        return SuspendOutcome(False, None, int(target_epoch), None, False, "none", "missing-backend")

    if lead < floor:
        _log("suspend_skipped", {"cycle_id": cycle_id, "reason": "insufficient-lead", "lead": lead, "min_lead": floor})
        return SuspendOutcome(False, None, int(target_epoch), None, False, "none", "insufficient-lead")

    arm = arm_rtc_wake(target_epoch, who, env)
    _log("wake_armed", {"cycle_id": cycle_id, "armed": arm.armed, "method": arm.method, "confirmed": arm.confirmed, "unit": arm.unit, "target_epoch": int(target_epoch)})
    if not arm.armed:
        # Never suspend with no wake armed -- that is exactly how a box ends up
        # asleep until someone touches it. Fall back to an awake wait instead.
        _log("suspend_skipped", {"cycle_id": cycle_id, "reason": "arm-failed"})
        return SuspendOutcome(False, None, int(target_epoch), None, False, arm.method, "arm-failed")

    dog: Watchdog | None = None
    if watchdog:
        dog = Watchdog(env)
        # Span the WHOLE expected sleep plus a resume margin. We cannot pet the
        # watchdog while suspended, so on hardware whose watchdog keeps counting
        # across S3 a short timeout would wrongly reboot a perfectly healthy long
        # suspend. Spanning the sleep means it only bites when the resume itself
        # runs long (wedged). (Many watchdogs instead stop across suspend and
        # resume counting on wake, where any reasonable timeout also works. The
        # ioctl is best-effort: a device that cannot accept this timeout keeps its
        # own default.)
        margin = suspend_drift_tolerance(env) * 4
        armed_ok = dog.arm(max(60, lead + margin))
        _log("watchdog", {"cycle_id": cycle_id, "armed": armed_ok, "device": watchdog_device_path(env), "timeout": max(60, lead + margin)})
        if not armed_ok:
            dog = None

    ledger_record_start(ledger, cycle_id, int(target_epoch), env, who=who, method=arm.method, confirmed=arm.confirmed, watchdog=bool(dog))
    _log("suspend_request", {"cycle_id": cycle_id, "method": arm.method, "target_epoch": int(target_epoch)})

    subprocess.run(["sync"], check=False)
    subprocess.run(["systemctl", "suspend"], check=False)

    # systemctl suspend returns asynchronously; poll the wall clock so we proceed
    # exactly when real time reaches the target (the RTC wake), not before.
    wall_clock_wait_until(int(target_epoch), env)
    if dog is not None:
        dog.keepalive()
        dog.disarm()

    woke = now_epoch(env)
    drift = woke - int(target_epoch)
    reliable = abs(drift) <= suspend_drift_tolerance(env)
    ledger_record_done(ledger, cycle_id, woke, reliable, env, drift_seconds=drift, method=arm.method)
    trim_ledger(ledger)
    _log("suspend_resumed", {"cycle_id": cycle_id, "woke_epoch": woke, "drift_seconds": drift, "reliable": reliable, "method": arm.method})
    return SuspendOutcome(True, woke, int(target_epoch), drift, reliable, arm.method, "ok")
