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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.request import Request, urlopen


AUTONOMY_ABORT_STATUS = 75
TRANSIENT_COPILOT_CACHE_REASONS = {"capture-error", "format-changed", "refresh-pending", "timeout"}


def err(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def cache_root(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    base = env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "llm-tools"


def migrate_legacy_cache_dirs(env: dict[str, str] | None = None) -> None:
    env = env or os.environ
    legacy_root = Path(env.get("XDG_CACHE_HOME") or str(Path.home() / ".cache"))
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


def scheduler_log_dir(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "llm-scheduler" / "logs"


def ralph_log_dir(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "ralph-robin" / "logs"


def ralph_state_file(env: dict[str, str] | None = None) -> Path:
    return cache_root(env) / "ralph-robin" / "state.json"


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
            "extra_usage": obj.get("extra_usage"),
        }
    if not isinstance(rl, dict):
        return None
    primary = rl.get("five_hour") or rl.get("fiveHour") or rl.get("primary")
    secondary = rl.get("seven_day") or rl.get("sevenDay") or rl.get("weekly") or rl.get("secondary")
    percent_keys = ("used_percentage", "usedPercent", "used_percent", "utilization")
    return {
        "provider": "claude",
        "source": source,
        "plan": None,
        "five_hour": window_from(primary, 300, percent_keys) if isinstance(primary, dict) else None,
        "week": window_from(secondary, 10080, percent_keys) if isinstance(secondary, dict) else None,
    }


def latest_matching_line(root: Path, predicate: Any, env: dict[str, str] | None = None) -> str | None:
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
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-tail_lines:]
        except OSError:
            continue
        for line in reversed(lines):
            obj = read_json_text(line)
            if obj is not None and predicate(obj):
                return line
    return None


def read_codex(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = env or os.environ
    root = Path.home() / ".codex" / "sessions"
    line = latest_matching_line(root, lambda o: get_path(o, (("rate_limits",), ("rateLimits",), ("rateLimits", "rateLimits"), ("msg", "rate_limits"), ("msg", "rateLimits"), ("payload", "rate_limits"), ("payload", "rateLimits"))) is not None, env)
    if not line:
        return None
    return normalize_codex_obj(json.loads(line), "~/.codex/sessions")


def read_claude_api(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = env or os.environ
    cache = usage_cache_dir(env) / "claude-usage-api.json"
    cred = Path.home() / ".claude" / ".credentials.json"
    token = ""
    try:
        token = json.loads(cred.read_text(encoding="utf-8")).get("claudeAiOauth", {}).get("accessToken", "")
    except OSError:
        pass
    except json.JSONDecodeError:
        pass
    if token:
        req = Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )
        try:
            with urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8", "replace")
            if text:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
                return normalize_claude_obj(json.loads(text), "api.anthropic.com/api/oauth/usage")
        except Exception:
            pass
    if cache.is_file() and cache.stat().st_size > 0:
        try:
            return normalize_claude_obj(json.loads(cache.read_text(encoding="utf-8")), str(cache))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def read_claude(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    env = env or os.environ
    api = read_claude_api(env)
    if api:
        return api
    status_cache = usage_cache_dir(env) / "claude-status.json"
    if status_cache.is_file() and status_cache.stat().st_size > 0:
        try:
            norm = normalize_claude_obj(json.loads(status_cache.read_text(encoding="utf-8")), str(status_cache))
            if norm:
                return norm
        except (OSError, json.JSONDecodeError):
            pass
    root = Path.home() / ".claude" / "projects"
    line = latest_matching_line(root, lambda o: get_path(o, (("rate_limits",), ("rateLimits",), ("message", "rate_limits"), ("message", "rateLimits"))) is not None, env)
    if not line:
        return None
    return normalize_claude_obj(json.loads(line), "~/.claude/projects")


def find_copilot_cli() -> str | None:
    return shutil.which("copilot") or shutil.which("github-copilot")


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
    cli = find_copilot_cli()
    if not cli:
        return "missing-cli", ""
    capture_cwd = env.get("LLM_USAGE_COPILOT_CWD") or str(Path(__file__).resolve().parent.parent)
    helper_cmd = env.get("LLM_USAGE_COPILOT_CAPTURE_CMD", "")
    timeout_seconds = int(env.get("LLM_USAGE_COPILOT_TIMEOUT", "10") or "10")
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
    m = re.search(r"(?:Monthly|Plan):\s*([0-9]+(?:[.][0-9]+)?)%\s*used", text)
    return float(m.group(1)) if m else None


def parse_copilot_ai_credits(text: str) -> float | None:
    m = re.search(r"AI\s+Credits:\s*([0-9]+(?:[.][0-9]+)?)", text)
    return float(m.group(1)) if m else None


def read_copilot_live(env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    status, screen = capture_copilot_screen(env)
    monthly_used = parse_copilot_monthly_used(screen)
    ai_credits = parse_copilot_ai_credits(screen)
    if monthly_used is not None or ai_credits is not None:
        return {
            "provider": "copilot",
            "source": "copilot cli",
            "capture_status": status,
            "monthly": None
            if monthly_used is None
            else {"used": monthly_used, "remaining": min(100.0, max(0.0, 100.0 - monthly_used))},
            "ai_credits": None if ai_credits is None else {"used": ai_credits},
        }
    reason = status
    if "trust_prompt_seen" in screen:
        reason = "trust-prompt"
    elif re.search(r"[Ll]og\s*-?\s*[Ii]n|[Aa]uth", screen):
        reason = "not-authenticated"
    elif screen:
        reason = "format-changed"
    return {"provider": "copilot", "source": "copilot cli", "available": False, "reason": reason}


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
            stale_after = int(env.get("LLM_USAGE_COPILOT_TIMEOUT", "10") or "10") + 30
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
    wait_budget = float(env.get("LLM_USAGE_COPILOT_REFRESH_WAIT", "1") or "1")
    deadline = time.time() + max(0.0, wait_budget)
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
    if cache.is_file() and cache.stat().st_size > 0:
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
    return {"provider": "copilot", "source": "copilot cli", "available": False, "reason": "refresh-pending"}


def decorate_window(window: dict[str, Any] | None) -> dict[str, Any] | None:
    if window is None:
        return None
    out = dict(window)
    out["remaining"] = remaining_from_used(out.get("used"))
    return out


def json_for_provider(provider_json: dict[str, Any] | None, provider: str) -> dict[str, Any]:
    if not provider_json:
        return {"provider": provider, "available": False}
    out = dict(provider_json)
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


def estimate_remaining_time_from_log(provider: str, window: str, remaining: Any, env: dict[str, str] | None = None) -> str:
    env = env or os.environ
    rem = num(remaining)
    if rem is None or rem <= 0:
        return "-"
    path = usage_cache_dir(env) / "llm-usage.log"
    if not path.is_file() or path.stat().st_size == 0:
        return "-"
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
    now = now_epoch(env)
    cutoff = now - lookback
    samples: list[tuple[int, float]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-usage_log_tail_lines(env):]
    except OSError:
        return "-"
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
        return "-"
    if max_stale > 0 and now - samples[-1][0] > max_stale:
        return "-"
    # Refuse to extrapolate a window-scale ETA from a sliver of history: a single
    # coarse step (e.g. a stale reading jumping to the current value) would
    # otherwise be read as a sustained burn rate and produce a wildly short
    # estimate. Require the observed span to cover a meaningful fraction of the
    # window before reporting anything.
    observed_span = samples[-1][0] - samples[0][0]
    window_seconds = REMAINING_TIME_WINDOW_SECONDS.get(window, 0)
    min_span = max(min_span_floor, int(window_seconds * min_span_fraction))
    if observed_span < min_span:
        return "-"
    prev_ts: int | None = None
    prev_rem = 0.0
    total_reduction = 0.0
    total_seconds = 0
    for ts, value in samples:
        if prev_ts is not None:
            dt = ts - prev_ts
            # Increases are window resets and gaps longer than max_gap may hide a
            # reset; skip those intervals but keep the burn accumulated so far.
            if dt > 0 and (max_gap <= 0 or dt <= max_gap) and value <= prev_rem:
                total_reduction += prev_rem - value
                total_seconds += dt
        prev_ts = ts
        prev_rem = value
    if total_seconds <= 0 or total_reduction <= 0:
        return "-"
    remaining_seconds = int(rem * total_seconds / total_reduction)
    if remaining_seconds <= 0:
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


def validate_tool_window(tool: str, window: str) -> None:
    if window not in {"auto", "5h", "weekly", "monthly"}:
        err(f"invalid --window: {window}")
        raise SystemExit(2)
    valid = {
        "codex": {"auto", "5h", "weekly"},
        "claude": {"auto", "5h", "weekly"},
        "copilot": {"auto", "monthly"},
    }
    if window not in valid.get(tool, set()):
        if tool == "copilot":
            err(f"--window {window} is not valid for copilot (use auto or monthly)")
        else:
            err(f"--window {window} is not valid for {tool} (use auto, 5h, or weekly)")
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


def setup_run_logs(log_dir: Path, suffix: str, tool_link: str = "", run_dir: Path | None = None) -> RunLogs:
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
        if tool_link:
            _symlink(run, log_dir / f"latest-{tool_link}")
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


def usage_snapshot_for_tool(tool: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    injected = env.get("LLM_SCHEDULER_USAGE_JSON")
    if injected:
        raw = json.loads(injected)
        if isinstance(raw, dict) and tool in raw:
            return raw[tool]
        return raw
    if tool == "codex":
        return json_for_provider(read_codex(env), "codex")
    if tool == "claude":
        return json_for_provider(read_claude(env), "claude")
    if tool == "copilot":
        return json_for_copilot(read_copilot(env), False)
    return {"provider": tool, "available": False, "reason": "unsupported-tool"}


def usage_decision_for_tool(tool: str, window: str, min_remaining: str, poll_interval: str, snapshot: dict[str, Any], env: dict[str, str] | None = None) -> dict[str, Any]:
    env = env or os.environ
    now = now_epoch(env)
    poll = int(poll_interval)
    minimum = float(min_remaining)

    def win(name: str, obj: Any) -> dict[str, Any] | None:
        if not isinstance(obj, dict):
            return None
        reset = obj.get("resets_at")
        return {
            "name": name,
            "remaining": num(obj.get("remaining")),
            "resets_at": reset,
            "reset_epoch": parse_epoch(reset),
        }

    if tool == "copilot":
        if window in {"auto", "monthly"}:
            reset = copilot_monthly_reset_epoch(env)
            monthly = snapshot.get("monthly") if isinstance(snapshot, dict) else None
            windows = [
                {
                    "name": "monthly",
                    "remaining": num(monthly.get("remaining")) if isinstance(monthly, dict) else None,
                    "resets_at": str(reset) if reset is not None else None,
                    "reset_epoch": reset,
                }
            ]
        else:
            windows = []
    elif window == "auto":
        windows = [x for x in (win("5h", snapshot.get("five_hour")), win("weekly", snapshot.get("week"))) if x is not None]
    elif window == "5h":
        windows = [x for x in (win("5h", snapshot.get("five_hour")),) if x is not None]
    elif window == "weekly":
        windows = [x for x in (win("weekly", snapshot.get("week")),) if x is not None]
    else:
        windows = []
    known = [w for w in windows if w.get("remaining") is not None]
    exhausted = [
        w
        for w in known
        if w["remaining"] is not None
        and w["remaining"] <= minimum
        and (w.get("reset_epoch") is None or int(w["reset_epoch"]) > now)
    ]
    future_resets = [int(w["reset_epoch"]) for w in exhausted if w.get("reset_epoch") is not None and int(w["reset_epoch"]) > now]
    if snapshot.get("available") is False:
        return {"tool": tool, "usable": False, "reason": snapshot.get("reason", "unavailable"), "wait_until": now + poll, "windows": windows}
    if not windows:
        return {"tool": tool, "usable": False, "reason": "unsupported-window", "wait_until": now + poll, "windows": windows}
    if not known:
        return {"tool": tool, "usable": False, "reason": "inconclusive-usage", "wait_until": now + poll, "windows": windows}
    if exhausted:
        return {
            "tool": tool,
            "usable": False,
            "reason": "rate-limited",
            "wait_until": max(future_resets) if future_resets else now + poll,
            "windows": windows,
            "exhausted": exhausted,
        }
    return {"tool": tool, "usable": True, "reason": "usable", "wait_until": None, "windows": windows}


def argv_to_command_line(argv: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def template_argv(template: str, *, tool: str, prompt: str, prompt_file: Path, cwd: str) -> list[str]:
    parts = shlex.split(template)
    values = {"{tool}": tool, "{prompt}": prompt, "{prompt_file}": str(prompt_file), "{cwd}": cwd}
    out = []
    for part in parts:
        for key, value in values.items():
            part = part.replace(key, value)
        out.append(part)
    return out


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
    safe_prompts = ("Confirm folder trust", "Do you trust the files in this folder?")
    blocking_patterns = (
        re.compile(r"\bwhat do you want to do\?", re.I),
        re.compile(r"\benter to confirm\b", re.I),
        re.compile(r"\besc to cancel\b", re.I),
        re.compile(r"\buse (?:the )?arrow keys\b", re.I),
        re.compile(r"\bpress (?:enter|return) to\b", re.I),
        re.compile(r"\badjust monthly spend limit\b", re.I),
        re.compile(r"\bwait for limit to reset\b", re.I),
        re.compile(r"\bupgrade to max\b", re.I),
        re.compile(r"\byou(?:'|\u2019)ve hit your monthly spend limit\b", re.I),
        re.compile(r"\bmonthly spend limit\b", re.I),
        re.compile(r"\brate[- ]limit options\b", re.I),
        re.compile(r"\b(?:proceed|continue)\?\s*(?:\[[yYnN]/[yYnN]\]|\([yYnN]/[yYnN]\))", re.I),
    )
    question_line = re.compile(
        r"(?im)^\s*(?:[>\-\*\d.)\s]*)?(?:what|which|who|when|where|why|how|do you|would you|should i|should we|can i|may i|please (?:choose|select)|select|choose|confirm|proceed|continue)\b[^\n?]{0,180}\?\s*$"
    )
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


def output_is_retryable(status: int, output: str, attached: bool = False) -> bool:
    if attached:
        return status not in (0, 130, 143)
    if status != 0:
        return True
    return bool(
        re.search(
            r"rate[ _-]?limit (exceeded|reached|hit)|too many requests|http[ /?]429|status 429|429 too many requests|quota (exceeded|reached)|usage limit (exceeded|reached)|overloaded|service unavailable|temporarily unavailable|try again later",
            output,
            re.I,
        )
    )


def wake_diagnostics() -> dict[str, Any]:
    user_systemd = "unknown"
    if have_cmd("systemctl"):
        proc = subprocess.run(["systemctl", "--user", "is-system-running"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False)
        state = proc.stdout.strip()
        user_systemd = state or "unknown"
    return {
        "systemd_run": have_cmd("systemd-run"),
        "rtcwake": have_cmd("rtcwake"),
        "user_systemd": user_systemd,
        "note": "wake is best effort and depends on firmware, kernel, RTC, and systemd support",
    }
