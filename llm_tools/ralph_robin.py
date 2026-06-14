from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import common
from . import config as toolconfig
from . import scheduler


APP_NAME = "ralph-robin"

# A provider that "succeeds" this many times in a row faster than the minimum
# iteration floor is not doing real work (instant no-op / misconfiguration). The
# orchestrator stops rather than spin or burn quota forever.
FAST_SUCCESS_ABORT_STREAK = 5

# A hard provider failure (non-zero, non-abort exit) rotates to the next provider
# instead of killing the persistent loop. Only a sustained streak with no
# successful increment in between — a permanently broken setup — is fatal.
HARD_FAIL_ABORT_STREAK = 6


USAGE = """Usage: ralph-robin
  ralph-robin (-p TEXT | -f FILE) [options]

Round-robin prompt submission across local LLM CLIs. By default it prefers the
provider with the highest remaining daily capacity (reset-window or budget
remaining broken down over the days until reset) among providers that are
currently usable, so weekly/budget quotas burn down more evenly without
idling on a rate-limited provider. Disable this with
--no-even-burn to keep using the current provider until it is exhausted.

ralph-robin runs a persistent loop and owns the orchestration: it picks a
provider, submits the prompt for one increment, and when that provider's CLI
exits cleanly it makes a fresh routing decision and submits again (e.g. Claude
-> ralph-robin -> Claude). When every configured provider is rate-limited it
does not stop; it suspends the computer with an RTC wake-up timer set to the
earliest provider window renewal, then on wake resumes this same loop and
re-evaluates the rotation (falling back to an in-process wait when suspend is
unavailable). The loop ends only after --max-duration (default 24h) or
--max-iterations increments, on a non-recoverable failure, or if a provider
keeps returning instant successes without doing work. Use --max-iterations 1
for the legacy single-shot behavior.

By default the selected CLI uses llm-scheduler's autonomous headless adapter
even from an interactive terminal. This avoids provider prompts blocking the
rotation. Use llm-scheduler directly for an attached interactive run.

Examples:
  ralph-robin --prompt-file task.md
  ralph-robin --prompt "Continue until tests pass"
  ralph-robin --providers claude,codex,copilot,kilo,opencode,minimax --prompt-file task.md
  ralph-robin --prompt-file task.md --tmux llm-work
  ralph-robin --prompt-file task.md --dry-run

Options:
  -P, --providers LIST                     Providers in rotation (default: claude,codex).
  -p, --prompt TEXT                        Prompt text.
  -f, --prompt-file FILE                   Read prompt from FILE, preserving content.
  -s, --scope SCOPE                        Capacity scope to gate on (default: auto).
  -W, --window SCOPE                       Deprecated alias for --scope.
  -m, --min-remaining PERCENT              Minimum required remaining percentage (default: 1).
  -i, --poll-interval SECONDS              Poll interval passed to llm-scheduler (default: 60).
  -u, --max-unavailable-wait SECONDS       Bound inconclusive usage waits before optimistic launch.
  -r, --retry-delays LIST                  Comma-separated retry delays (default: 60,180,600).
  -R, --no-retry                           Disable retries after failed submission.
  -e, --even-burn                          Prefer highest remaining daily capacity (default).
  -E, --no-even-burn                       Keep using current provider until exhausted.
  -n, --max-iterations N                   Stop after N successful increments (0 means no limit).
  -D, --max-duration DURATION              Stop after duration like 24h, 90m, 30s (default: 24h).
  -I, --min-iteration-seconds N            Floor on successive increment runtime (default: 5).
  -x, --prefix LIST                        Prefix relayed lines with fields: time, provider, usage.
  -X, --prefix-usage-interval SECONDS      Refresh interval for cached prefix usage field.
  -C, --cwd DIR                            Working directory for target CLI.
  -F, --fresh                              Launch a fresh CLI process through llm-scheduler.
  -H, --headless                           Use non-interactive provider command on captured PTY.
  -T, --tmux SESSION[:WINDOW]              Execute through tmux via llm-scheduler.
  -g, --command-template TEMPLATE          Override provider command; placeholders: {provider}, {prompt}, {prompt_file}, {cwd}.
  -y, --auto-confirm                       Acknowledge only known safe prompts (default).
  -Y, --no-auto-confirm                    Disable automatic prompt acknowledgement.
  -q, --headless-idle-timeout SECONDS      Abort headless runs with no output progress (0 disables).
  -Q, --headless-question-timeout SECONDS  Abort headless runs that ask a question then stall.
  -L, --log-dir DIR                        Log directory.
  -S, --state-file FILE                    Rotation state file.
  -k, --wake                               Pass best-effort wake scheduling to llm-scheduler.
  -U, --suspend-until-ready                Suspend even for selected provider wait gates.
  -d, --dry-run                            Resolve rotation and usage state without submitting.
  -h, --help                               Show this help.

Providers: codex, claude, copilot, kilo, opencode, minimax.
Scopes: auto, 5h, weekly, monthly, balance, budget, byok, ungated.
"""


@dataclass
class RalphConfig:
    providers_spec: str = "claude,codex"
    providers: list[str] = field(default_factory=list)
    prompt_text: str = ""
    prompt_file: str = ""
    prompt_source: str = ""
    scope: str = "auto"
    min_remaining: str = "1"
    poll_interval: str = "60"
    max_unavailable_wait: str = "900"
    retry_delays: str = "60,180,600"
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    exec_mode: str = "fresh"
    tmux_target: str = ""
    command_template: str = ""
    auto_confirm: bool = True
    headless: bool = True
    log_dir: Path = field(default_factory=common.ralph_log_dir)
    state_file: Path = field(default_factory=common.ralph_state_file)
    wake: bool = False
    suspend_until_ready: bool = False
    dry_run: bool = False
    even_burn: bool = True
    max_iterations: str = "0"
    max_duration: str = "24h"
    min_iteration_seconds: str = "5"
    prefix_spec: str = "time,provider"
    prefix_fields: list[str] = field(default_factory=list)
    prefix_usage_interval: str = "15"
    # Per-provider routing policies (model + allow_fallback) resolved from the
    # shared config file, keyed by provider name.
    policies: dict[str, toolconfig.ProviderPolicy] = field(default_factory=dict)
    # CLI flags the user passed explicitly, so config-file values never clobber
    # them (precedence: built-in defaults < config file < CLI flags).
    explicit: set[str] = field(default_factory=set)


def trim(value: str) -> str:
    return value.strip()


def parse_duration(text: str) -> int | None:
    """Parse a duration like 24h, 90m, 30s, 1d, 1.5h, or bare seconds.

    Returns the number of seconds (0 means "no limit"), or None if invalid.
    """
    s = text.strip().lower()
    if s == "":
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    mult = 1
    if s[-1] in units:
        mult = units[s[-1]]
        s = s[:-1]
    try:
        value = float(s)
    except ValueError:
        return None
    if value < 0:
        return None
    return int(value * mult)


def monotonic() -> float:
    return time.monotonic()


def sleep_seconds(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def color_enabled() -> bool:
    return bool(
        sys.stderr.isatty()
        and os.environ.get("TERM") != "dumb"
        and not os.environ.get("NO_COLOR")
        and not os.environ.get("LLM_USAGE_NO_COLOR")
    )


def style(text: str, role: str) -> str:
    return common.ansi_wrap(text, role) if color_enabled() else text


def status_line(message: str, *, level: str = "info") -> None:
    role = level if level in common.ANSI_COLOR_ROLES else "info"
    stamp = time.strftime("[%H:%M:%S] ")
    prefix = style(f"{common.symbol_prefix('brand')}ralph-robin", "brand")
    marker = common.symbol_prefix(role).rstrip()
    marker_text = f"{style(marker, role)} " if marker else ""
    print(f"{style(stamp, 'dim')}{prefix}: {marker_text}{message}", file=sys.stderr)


def decision_summary(decision: dict[str, Any]) -> str:
    reason = str(decision.get("reason", "unknown"))
    windows = decision.get("windows")
    parts: list[str] = []
    if isinstance(windows, list):
        for window in windows:
            if not isinstance(window, dict):
                continue
            name = window.get("name", "?")
            kind = window.get("kind") or "reset_window"
            if kind == "balance":
                amount = window.get("remaining_amount")
                currency = window.get("currency") or ""
                if amount is not None:
                    if currency:
                        parts.append(f"{name} {currency}{common.fmt_number(amount)} left")
                    else:
                        parts.append(f"{name} {common.fmt_number(amount)} left")
                continue
            if kind == "ungated":
                text = window.get("label") or name
                parts.append(f"{name} {text}")
                continue
            remaining = window.get("remaining")
            if isinstance(remaining, (int, float)):
                parts.append(f"{name} {common.fmt_pct(remaining)}% left")
    wait_until = decision.get("wait_until")
    if reason == "rate-limited" and isinstance(wait_until, int):
        parts.append(f"until {common.format_local_epoch(wait_until)}")
    elif reason == "budget-exhausted" and isinstance(wait_until, int):
        parts.append(f"reset at {common.format_local_epoch(wait_until)}")
    elif reason == "insufficient-balance":
        parts.append("add balance")
    detail = ", ".join(parts) if parts else "-"
    return f"{reason} ({detail})"


def print_usage_summary(selection: dict[str, Any]) -> None:
    decisions = selection.get("decisions")
    if not isinstance(decisions, list):
        return
    rendered: list[str] = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider", "?"))
        summary = decision_summary(item)
        if item.get("usable") is True:
            rendered.append(f"{style(provider, 'ok')}: {summary}")
        elif item.get("reason") == "rate-limited":
            rendered.append(f"{style(provider, 'error')}: {summary}")
        else:
            rendered.append(f"{style(provider, 'warn')}: {summary}")
    status_line("usage " + " | ".join(rendered), level="dim")


def ralph_runtime_context(cfg: RalphConfig, selected_provider: str, selection: dict[str, Any]) -> str:
    decisions = selection.get("decisions")
    summaries: list[str] = []
    if isinstance(decisions, list):
        for item in decisions:
            if isinstance(item, dict):
                summaries.append(f"- {item.get('provider', '?')}: {decision_summary(item)}")
    decision_text = "\n".join(summaries) if summaries else "- unavailable"
    return (
        "RALPH ROBIN RUNTIME CONTEXT\n"
        "This block is injected by ralph-robin and takes precedence for scheduling, handoff, and capacity decisions.\n"
        f"- Current selected provider: {selected_provider}\n"
        f"- Configured provider rotation: {', '.join(cfg.providers)}\n"
        "- Treat any original prompt instruction to check or schedule a different provider as stale unless Ralph's latest decisions show the current provider is unusable.\n"
        f"- For stop thresholds such as session window, credits, balance, budget, or below 25%, evaluate the current selected provider ({selected_provider}) and its current scopes (balance, budget, ungated, or reset window), not a previously-used provider named in the prompt.\n"
        "- Do not run provider-specific llm-scheduler --suspend-until-ready commands from the original prompt while the current selected provider is usable; Ralph owns cross-provider rotation and suspend decisions.\n"
        "- Latest Ralph capacity decisions:\n"
        f"{decision_text}\n"
        "END RALPH ROBIN RUNTIME CONTEXT\n"
    )


def provider_prompt_for(cfg: RalphConfig, selected_provider: str, selection: dict[str, Any], prompt: str) -> str:
    return f"{ralph_runtime_context(cfg, selected_provider, selection)}\n{prompt}"


def parse_providers(raw: str) -> list[str]:
    providers: list[str] = []
    for part in raw.split(","):
        provider = trim(part)
        if not provider:
            continue
        if provider not in {"codex", "claude", "copilot", "kilo", "opencode", "minimax"}:
            common.err(f"invalid provider in --providers: {provider}")
            raise SystemExit(2)
        providers.append(provider)
    if not providers:
        common.err("--providers must name at least one provider")
        raise SystemExit(2)
    return providers


# Tokens that disable the per-line prefix entirely (no fields, no brackets).
PREFIX_OFF_TOKENS = {"none", "off"}


def parse_prefix_fields(raw: str) -> list[str]:
    """Parse the --prefix value into an ordered list of prefix fields.

    Accepts a comma-separated combination of common.LINE_PREFIX_FIELDS, in any
    order, de-duplicated while preserving first occurrence. An empty value or a
    "none"/"off" token turns the prefix off entirely.
    """
    fields: list[str] = []
    for part in raw.split(","):
        token = trim(part).lower()
        if not token:
            continue
        if token in PREFIX_OFF_TOKENS:
            return []
        if token not in common.LINE_PREFIX_FIELDS:
            common.err(f"invalid field in --prefix: {token} (choose from {', '.join(common.LINE_PREFIX_FIELDS)}, or none)")
            raise SystemExit(2)
        if token not in fields:
            fields.append(token)
    return fields


def parse_args(argv: list[str]) -> RalphConfig:
    cfg = RalphConfig()
    i = 0
    while i < len(argv):
        arg = argv[i]
        def need_value(msg: str) -> str:
            nonlocal i
            if i + 1 >= len(argv):
                common.err(msg)
                raise SystemExit(2)
            value = argv[i + 1]
            i += 2
            return value

        if arg in ("-P", "--providers"):
            cfg.providers_spec = need_value("--providers requires a value")
            cfg.explicit.add("providers")
        elif arg in ("-p", "--prompt"):
            cfg.prompt_text = need_value("--prompt requires text")
            cfg.prompt_source = "inline"
        elif arg in ("-f", "--prompt-file"):
            cfg.prompt_file = need_value("--prompt-file requires a file")
            cfg.prompt_source = f"file:{cfg.prompt_file}"
        elif arg in ("-s", "--scope", "-W", "--window"):  # --window is a deprecated alias for --scope
            cfg.scope = need_value("--scope requires a value")
            cfg.explicit.add("scope")
        elif arg in ("-m", "--min-remaining"):
            cfg.min_remaining = need_value("--min-remaining requires a value")
            cfg.explicit.add("min_remaining")
        elif arg in ("-i", "--poll-interval"):
            cfg.poll_interval = need_value("--poll-interval requires seconds")
            cfg.explicit.add("poll_interval")
        elif arg in ("-u", "--max-unavailable-wait"):
            cfg.max_unavailable_wait = need_value("--max-unavailable-wait requires seconds")
            cfg.explicit.add("max_unavailable_wait")
        elif arg in ("-r", "--retry-delays"):
            cfg.retry_delays = need_value("--retry-delays requires a list")
            cfg.explicit.add("retry_delays")
        elif arg in ("-R", "--no-retry"):
            cfg.retry_delays = ""
            cfg.explicit.add("retry_delays")
            i += 1
        elif arg in ("-e", "--even-burn"):
            cfg.even_burn = True
            cfg.explicit.add("even_burn")
            i += 1
        elif arg in ("-E", "--no-even-burn"):
            cfg.even_burn = False
            cfg.explicit.add("even_burn")
            i += 1
        elif arg in ("-n", "--max-iterations"):
            cfg.max_iterations = need_value("--max-iterations requires a count")
            cfg.explicit.add("max_iterations")
        elif arg in ("-D", "--max-duration"):
            cfg.max_duration = need_value("--max-duration requires a duration")
            cfg.explicit.add("max_duration")
        elif arg in ("-I", "--min-iteration-seconds"):
            cfg.min_iteration_seconds = need_value("--min-iteration-seconds requires seconds")
            cfg.explicit.add("min_iteration_seconds")
        elif arg in ("-x", "--prefix"):
            cfg.prefix_spec = need_value("--prefix requires a value")
            cfg.explicit.add("prefix")
        elif arg in ("-X", "--prefix-usage-interval"):
            cfg.prefix_usage_interval = need_value("--prefix-usage-interval requires seconds")
            cfg.explicit.add("prefix_usage_interval")
        elif arg in ("-C", "--cwd"):
            cfg.cwd = need_value("--cwd requires a directory")
        elif arg in ("-F", "--fresh"):
            cfg.exec_mode = "fresh"
            cfg.tmux_target = ""
            i += 1
        elif arg in ("-H", "--headless"):
            cfg.headless = True
            i += 1
        elif arg in ("-T", "--tmux"):
            cfg.exec_mode = "tmux"
            cfg.tmux_target = need_value("--tmux requires SESSION[:WINDOW]")
        elif arg in ("-g", "--command-template"):
            cfg.command_template = need_value("--command-template requires a template")
        elif arg in ("-y", "--auto-confirm"):
            cfg.auto_confirm = True
            i += 1
        elif arg in ("-Y", "--no-auto-confirm"):
            cfg.auto_confirm = False
            i += 1
        elif arg in ("-q", "--headless-idle-timeout"):
            os.environ["LLM_SCHEDULER_IDLE_TIMEOUT"] = need_value("--headless-idle-timeout requires seconds")
        elif arg in ("-Q", "--headless-question-timeout"):
            os.environ["LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT"] = need_value("--headless-question-timeout requires seconds")
        elif arg in ("-L", "--log-dir"):
            cfg.log_dir = Path(need_value("--log-dir requires a directory"))
        elif arg in ("-S", "--state-file"):
            cfg.state_file = Path(need_value("--state-file requires a file"))
        elif arg in ("-k", "--wake"):
            cfg.wake = True
            i += 1
        elif arg in ("-U", "--suspend-until-ready"):
            cfg.suspend_until_ready = True
            cfg.wake = True
            i += 1
        elif arg in ("-d", "--dry-run"):
            cfg.dry_run = True
            i += 1
        elif arg in ("-h", "--help"):
            print(USAGE, end="")
            raise SystemExit(0)
        else:
            common.err(f"unknown option: {arg}")
            print(USAGE, end="", file=sys.stderr)
            raise SystemExit(2)
    return cfg


# Config keys (merged [defaults] + [ralph]) mapped to (RalphConfig attr, kind).
_RALPH_CONFIG_FIELDS: dict[str, tuple[str, str]] = {
    "providers": ("providers_spec", "list"),
    "scope": ("scope", "str"),
    "min_remaining": ("min_remaining", "str"),
    "poll_interval": ("poll_interval", "str"),
    "max_unavailable_wait": ("max_unavailable_wait", "str"),
    "retry_delays": ("retry_delays", "str"),
    "even_burn": ("even_burn", "bool"),
    "max_iterations": ("max_iterations", "str"),
    "max_duration": ("max_duration", "str"),
    "min_iteration_seconds": ("min_iteration_seconds", "str"),
    "prefix": ("prefix_spec", "str"),
    "prefix_usage_interval": ("prefix_usage_interval", "str"),
}


def apply_config(cfg: RalphConfig, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Fill config-file values for flags not passed explicitly; return the config.

    Precedence: built-in defaults < config file < CLI flags. Per-provider
    policies are resolved later (in :func:`main`) once the provider list is
    parsed.
    """
    conf = toolconfig.load_config(env)
    if not conf:
        return conf
    tool = toolconfig.merged_tool_config(conf, "ralph")
    for key, (attr, kind) in _RALPH_CONFIG_FIELDS.items():
        if key in cfg.explicit or tool.get(key) is None:
            continue
        value = tool[key]
        if kind == "bool":
            setattr(cfg, attr, bool(value))
        elif kind == "list":
            setattr(cfg, attr, ",".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value))
        else:
            setattr(cfg, attr, "true" if value is True else "false" if value is False else str(value))
    return conf


def resolve_policies(cfg: RalphConfig, conf: dict[str, Any]) -> None:
    """Resolve a per-provider :class:`ProviderPolicy` for each rotation member."""
    policies: dict[str, toolconfig.ProviderPolicy] = {}
    for provider in cfg.providers:
        policy = toolconfig.provider_policy(conf, provider)
        if policy.model and provider not in scheduler.MODEL_FLAG_PROVIDERS:
            common.err(f"warning: model pinning is not supported for provider '{provider}'; ignoring model={policy.model}")
            policy = toolconfig.ProviderPolicy(model=None, allow_fallback=policy.allow_fallback, scope=policy.scope, min_remaining=policy.min_remaining)
        policies[provider] = policy
    cfg.policies = policies


def validate_args(cfg: RalphConfig) -> None:
    cfg.providers = parse_providers(cfg.providers_spec)
    common.validate_prompt_args(cfg.prompt_text, cfg.prompt_file)
    for provider in cfg.providers:
        common.validate_provider_scope(provider, cfg.scope)
    common.validate_gate_args(cfg.cwd, cfg.min_remaining, cfg.poll_interval, cfg.max_unavailable_wait, cfg.retry_delays)
    if not common.is_integer(cfg.max_iterations) or int(cfg.max_iterations) < 0:
        common.err("--max-iterations must be a non-negative integer")
        raise SystemExit(2)
    if parse_duration(cfg.max_duration) is None:
        common.err("--max-duration must be a duration like 24h, 90m, 30s, or seconds")
        raise SystemExit(2)
    if not common.is_integer(cfg.min_iteration_seconds) or int(cfg.min_iteration_seconds) < 0:
        common.err("--min-iteration-seconds must be a non-negative integer")
        raise SystemExit(2)
    cfg.prefix_fields = parse_prefix_fields(cfg.prefix_spec)
    if not common.is_integer(cfg.prefix_usage_interval) or int(cfg.prefix_usage_interval) < 0:
        common.err("--prefix-usage-interval must be a non-negative integer")
        raise SystemExit(2)
    if not common.is_integer(os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600")):
        common.err("LLM_SCHEDULER_IDLE_TIMEOUT must be integer seconds")
        raise SystemExit(2)
    if not common.is_integer(os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30")):
        common.err("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT must be integer seconds")
        raise SystemExit(2)


def safe_args_json(cfg: RalphConfig) -> dict[str, Any]:
    return {
        "providers_spec": cfg.providers_spec,
        "providers": cfg.providers,
        "policies": {p: {"model": pol.model, "allow_fallback": pol.allow_fallback} for p, pol in cfg.policies.items()},
        "scope": cfg.scope,
        "min_remaining": float(cfg.min_remaining),
        "poll_interval": int(cfg.poll_interval),
        "max_unavailable_wait": int(cfg.max_unavailable_wait),
        "retry_delays": cfg.retry_delays,
        "cwd": cfg.cwd,
        "mode": cfg.exec_mode,
        "tmux": cfg.tmux_target,
        "prompt_source": cfg.prompt_source,
        "log_dir": str(cfg.log_dir),
        "state_file": str(cfg.state_file),
        "headless_idle_timeout": int(os.environ.get("LLM_SCHEDULER_IDLE_TIMEOUT", "600")),
        "headless_question_timeout": int(os.environ.get("LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT", "30")),
        "auto_confirm": cfg.auto_confirm,
        "headless": cfg.headless,
        "dry_run": cfg.dry_run,
        "wake": cfg.wake,
        "suspend_until_ready": cfg.suspend_until_ready,
        "even_burn": cfg.even_burn,
        "max_iterations": int(cfg.max_iterations),
        "max_duration_seconds": parse_duration(cfg.max_duration) or 0,
        "min_iteration_seconds": int(cfg.min_iteration_seconds),
        "prefix_fields": list(cfg.prefix_fields),
        "prefix_usage_interval": int(cfg.prefix_usage_interval),
    }


def current_index_from_state(cfg: RalphConfig) -> int:
    if cfg.state_file.is_file() and cfg.state_file.stat().st_size > 0:
        try:
            obj = json.loads(cfg.state_file.read_text(encoding="utf-8"))
            if obj.get("providers_spec") == cfg.providers_spec:
                index = int(obj.get("current_index", 0))
            else:
                index = 0
        except (OSError, ValueError, json.JSONDecodeError):
            index = 0
    else:
        index = 0
    return index if 0 <= index < len(cfg.providers) else 0


def save_state(cfg: RalphConfig, selected_index: int, selected_provider: str) -> None:
    if cfg.dry_run:
        return
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        cfg.state_file.parent.chmod(0o700)
    except OSError:
        pass
    obj = {
        "providers_spec": cfg.providers_spec,
        "providers": cfg.providers,
        "current_provider": selected_provider,
        "current_index": selected_index,
        "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).astimezone().isoformat(),
    }
    cfg.state_file.write_text(json.dumps(obj, separators=(",", ":")) + "\n", encoding="utf-8")
    try:
        cfg.state_file.chmod(0o600)
    except OSError:
        pass


def rotation_order_indices(length: int, current_index: int) -> list[int]:
    return [(current_index + i) % length for i in range(length)]


WEEKLY_WINDOW_DAYS = common.REMAINING_TIME_WINDOW_SECONDS["weekly"] / 86400.0


def _scope_pace_remaining(window: dict[str, Any], env: dict[str, str] | None = None) -> float | None:
    """Remaining-per-day for a single decision-window dict.

    Mirrors :func:`llm_tools.capacity.scope_pace` but consumes the legacy
    ``window`` shape (``name``, ``remaining``, ``reset_epoch``, ``kind``)
    produced by :func:`llm_tools.common.usage_decision_for_provider`. Reset
    windows and budget scopes are both rankable; balance and ungated are
    not.
    """
    kind = window.get("kind") or "reset_window"
    if kind in ("balance", "ungated", "unknown"):
        return None
    rem = window.get("remaining")
    if not isinstance(rem, (int, float)):
        return None
    env = env or os.environ
    reset_epoch = window.get("reset_epoch")
    if isinstance(reset_epoch, int) and reset_epoch > common.now_epoch(env):
        days = max((reset_epoch - common.now_epoch(env)) / 86400.0, 1.0)
    else:
        days = 7.0
    return float(rem) / days


def remaining_daily_capacity(decision: dict[str, Any], env: dict[str, str] | None = None) -> float | None:
    """Highest remaining-per-day across the decision's pace-rankable scopes.

    Reset-window and budget scopes are ranked. A provider that only exposes
    balance/ungated scopes returns ``None`` so even-burn falls back to a
    plain rotation. This replaces the old "weekly window only" logic so
    Kilo's budget scope can participate in even-burn when it is configured.
    """
    windows = decision.get("windows")
    if not isinstance(windows, list):
        return None
    best: float | None = None
    for window in windows:
        if not isinstance(window, dict):
            continue
        score = _scope_pace_remaining(window, env)
        if score is None:
            continue
        if best is None or score > best:
            best = score
    return best


def weekly_window_exhausted(decision: dict[str, Any]) -> bool:
    exhausted = decision.get("exhausted")
    if not isinstance(exhausted, list):
        return False
    return any(isinstance(window, dict) and window.get("name") == "weekly" for window in exhausted)


def decision_has_blocked_scope(decision: dict[str, Any], scope_name: str) -> bool:
    exhausted = decision.get("exhausted")
    if not isinstance(exhausted, list):
        return False
    return any(isinstance(window, dict) and window.get("name") == scope_name for window in exhausted)


def even_burn_candidate(decision: dict[str, Any]) -> bool:
    if decision.get("usable") is True:
        return True
    # A rate-limited or budget-exhausted decision can still be ranked
    # against the rest of the rotation by how much budget/weekly percent
    # remains per day, so even-burn can prefer the gentler provider.
    reason = decision.get("reason")
    if reason == "rate-limited" and not weekly_window_exhausted(decision):
        return True
    if reason == "budget-exhausted" and not decision_has_blocked_scope(decision, "budget"):
        return True
    return False


def even_burn_index(cfg: RalphConfig, decisions: list[dict[str, Any]], current_index: int, skipped: set[str]) -> int | None:
    ranked_indices = [
        i
        for i, decision in enumerate(decisions)
        if even_burn_candidate(decision) and cfg.providers[i] not in skipped
    ]
    if len(ranked_indices) < 2:
        return None
    ready_indices = [i for i in ranked_indices if decisions[i].get("usable") is True]
    candidate_indices = ready_indices if ready_indices else ranked_indices
    if len(candidate_indices) < 2:
        return None
    scored: list[tuple[float, int, int, int]] = []
    rotation_rank = {idx: rank for rank, idx in enumerate(rotation_order_indices(len(cfg.providers), current_index))}
    for i in candidate_indices:
        score = remaining_daily_capacity(decisions[i])
        if score is None:
            return None
        # When several providers are ready, tie-break toward the one that is
        # usable right now and closest in the configured rotation.
        usable = 1 if decisions[i].get("usable") is True else 0
        scored.append((score, usable, -rotation_rank[i], i))
    scored.sort(reverse=True)
    return scored[0][3] if scored else None


def select_provider(cfg: RalphConfig, logs: common.RunLogs, current_index: int, skipped: set[str]) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    for provider in cfg.providers:
        snapshot = common.usage_snapshot_for_provider(provider)
        policy = cfg.policies.get(provider)
        decision = common.usage_decision_for_provider(
            provider,
            cfg.scope,
            cfg.min_remaining,
            cfg.poll_interval,
            snapshot,
            model=policy.model if policy else None,
            allow_fallback=policy.allow_fallback if policy else True,
        )
        decisions.append(decision)
        common.log_event(logs, "usage_snapshot", {"provider": provider, "snapshot": snapshot})
        common.log_event(logs, "usage_decision", decision)
    if cfg.even_burn:
        balanced_index = even_burn_index(cfg, decisions, current_index, skipped)
        if balanced_index is not None:
            return {
                "index": balanced_index,
                "provider": cfg.providers[balanced_index],
                "rotation_reason": "even-burn",
                "all_rate_limited": False,
                "decision": decisions[balanced_index],
                "decisions": decisions,
            }
    if decisions[current_index].get("usable") is True and cfg.providers[current_index] not in skipped:
        return {"index": current_index, "provider": cfg.providers[current_index], "rotation_reason": "current-usable", "all_rate_limited": False, "decision": decisions[current_index], "decisions": decisions}
    for i in range(1, len(cfg.providers)):
        nxt = (current_index + i) % len(cfg.providers)
        if decisions[nxt].get("usable") is True and cfg.providers[nxt] not in skipped:
            return {"index": nxt, "provider": cfg.providers[nxt], "rotation_reason": "advanced-to-usable", "all_rate_limited": False, "decision": decisions[nxt], "decisions": decisions}

    for i in range(len(cfg.providers)):
        fallback = (current_index + i) % len(cfg.providers)
        if cfg.providers[fallback] in skipped:
            continue
        reason = decisions[fallback].get("reason")
        if reason not in ("rate-limited", "budget-exhausted", "insufficient-balance"):
            return {
                "index": fallback,
                "provider": cfg.providers[fallback],
                "rotation_reason": "advanced-to-undetermined",
                "all_rate_limited": False,
                "decision": decisions[fallback],
                "decisions": decisions,
            }

    active_decisions = [(i, decision) for i, decision in enumerate(decisions) if cfg.providers[i] not in skipped]
    blocked_reasons = {"rate-limited", "budget-exhausted", "insufficient-balance"}
    all_active_blocked = bool(active_decisions) and all(
        decision.get("reason") in blocked_reasons and isinstance(decision.get("wait_until"), int)
        for _i, decision in active_decisions
    )
    best_index = -1
    best_wait: int | None = None
    for i, decision in active_decisions:
        wait_until = decision.get("wait_until")
        if decision.get("reason") in blocked_reasons and isinstance(wait_until, int):
            if best_wait is None or wait_until < best_wait:
                best_wait = wait_until
                best_index = i
    if best_index == -1:
        for i in range(len(cfg.providers)):
            fallback = (current_index + i) % len(cfg.providers)
            if cfg.providers[fallback] not in skipped:
                best_index = fallback
                break
    if best_index == -1:
        return {"index": -1, "provider": "", "rotation_reason": "all-skipped", "all_rate_limited": False, "decision": {"usable": False, "reason": "all-skipped"}, "decisions": decisions}
    return {
        "index": best_index,
        "provider": cfg.providers[best_index],
        "rotation_reason": "all-unusable",
        "all_rate_limited": all_active_blocked and best_wait is not None,
        "decision": decisions[best_index],
        "decisions": decisions,
    }


def effective_model_for(cfg: RalphConfig, provider: str, decision: dict[str, Any]) -> str:
    """The model ralph should pin for ``provider`` on this iteration.

    Returns the policy's pinned model, except when fallback is allowed and the
    pinned model's own limit is exhausted — then it drops the pin (empty string)
    so the provider CLI picks an available model.
    """
    policy = cfg.policies.get(provider)
    if policy is None or not policy.model:
        return ""
    if policy.allow_fallback and decision.get("model_exhausted"):
        return ""
    return policy.model


def scheduler_config_for(cfg: RalphConfig, selected_provider: str, logs: common.RunLogs, provider_prompt: str, iteration: int, model: str = "") -> scheduler.SchedulerConfig:
    policy = cfg.policies.get(selected_provider)
    return scheduler.SchedulerConfig(
        provider=selected_provider,
        model=model,
        allow_fallback=policy.allow_fallback if policy else False,
        prompt_text=provider_prompt,
        prompt_source=f"ralph-runtime:{selected_provider}",
        scope=cfg.scope,
        min_remaining=cfg.min_remaining,
        poll_interval=cfg.poll_interval,
        max_unavailable_wait=cfg.max_unavailable_wait,
        retry_delays=cfg.retry_delays,
        cwd=cfg.cwd,
        exec_mode=cfg.exec_mode,
        tmux_target=cfg.tmux_target,
        command_template=cfg.command_template,
        auto_confirm=cfg.auto_confirm,
        headless=cfg.headless,
        log_dir=logs.run_dir,
        # Each iteration gets its own subdir so per-iteration provider logs
        # (attempt output, status, events) are preserved instead of overwritten.
        run_dir=logs.run_dir / f"iter-{iteration:03d}-{selected_provider}",
        dry_run=cfg.dry_run,
        wake=cfg.wake,
        # Ralph owns cross-provider suspend (see suspend_machine_until); it never
        # delegates the all-blocked suspend to llm-scheduler, whose resume would
        # wake into a single configured provider instead of the rotation.
        suspend_until_ready=cfg.suspend_until_ready,
        exact_stdout=True,
        claude_stream_json=selected_provider == "claude" and not cfg.command_template,
        ralph_robin_active=True,
        ralph_robin_providers=",".join(cfg.providers),
        # Stamp every relayed provider line with the configured marker (default
        # time + provider name) so a long, quiet increment is visibly distinguishable
        # from a wedged one and the active provider is always clear. See --prefix.
        # LLM_TOOLS_RALPH_NO_TIMESTAMPS=1 forces the marker off entirely (e.g. for
        # byte-exact piping), regardless of --prefix.
        output_prefix_fields=[] if os.environ.get("LLM_TOOLS_RALPH_NO_TIMESTAMPS", "0") == "1" else list(cfg.prefix_fields),
        output_prefix_usage_ttl=float(int(cfg.prefix_usage_interval)),
    )


def run_scheduler_inline(scfg: scheduler.SchedulerConfig) -> int:
    scheduler.resolve_attach_mode(scfg)
    child_logs = common.setup_run_logs(scfg.log_dir, scfg.provider or "wake", scfg.provider or "", scfg.run_dir)
    prompt, prompt_sha = common.load_prompt(scfg.prompt_text, scfg.prompt_file, child_logs)
    scfg.prompt_text = prompt
    common.log_text(child_logs, f"start provider={scfg.provider} cwd={scfg.cwd} attached={1 if scfg.attached else 0}")
    common.log_event(child_logs, "start", scheduler.safe_args_json(scfg))
    common.log_event(child_logs, "prompt", {"source": scfg.prompt_source, "sha256": prompt_sha, "prompt": prompt})
    try:
        scheduler.wait_until_usable(scfg, child_logs)
    except SystemExit as exc:
        return int(exc.code or 0)
    argv = scheduler.command_argv(scfg, child_logs, prompt)
    common.log_event(child_logs, "resolved_command", {"argv": argv})
    if scfg.dry_run:
        common.log_text(child_logs, "dry-run complete")
        common.log_event(child_logs, "final", {"status": "dry-run"})
        print("dry-run: no prompt submitted", file=sys.stderr)
        return 0
    delays = [int(x) for x in scfg.retry_delays.split(",") if x] if scfg.retry_delays else []
    attempt = 1
    result = scheduler.submit_once(scfg, child_logs, attempt, argv)
    if result == 0:
        common.log_event(child_logs, "final", {"status": "success"})
        return 0
    if result == common.AUTONOMY_ABORT_STATUS:
        common.log_event(child_logs, "final", {"status": "autonomy-abort"})
        return common.AUTONOMY_ABORT_STATUS
    for delay in delays:
        common.log_event(child_logs, "retry", {"after_attempt": attempt, "delay": delay})
        import time

        time.sleep(delay)
        attempt += 1
        result = scheduler.submit_once(scfg, child_logs, attempt, argv)
        if result == 0:
            common.log_event(child_logs, "final", {"status": "success"})
            return 0
        if result == common.AUTONOMY_ABORT_STATUS:
            common.log_event(child_logs, "final", {"status": "autonomy-abort"})
            return common.AUTONOMY_ABORT_STATUS
    common.log_event(child_logs, "final", {"status": "failed"})
    return 1


def soonest_wait_until(selection: dict[str, Any], env: dict[str, str] | None = None) -> int | None:
    """Earliest future reset epoch across the providers in this selection."""
    decisions = selection.get("decisions")
    if not isinstance(decisions, list):
        return None
    now = common.now_epoch(env)
    waits = [
        int(d["wait_until"])
        for d in decisions
        if isinstance(d, dict) and isinstance(d.get("wait_until"), int) and int(d["wait_until"]) > now
    ]
    return min(waits) if waits else None


def suspend_until_available(
    cfg: RalphConfig,
    logs: common.RunLogs,
    selection: dict[str, Any],
    start_monotonic: float,
    max_duration: int,
    reason: str,
) -> bool:
    """Wait for a blocked provider to free up instead of giving up.

    Sleeps until the soonest provider reset (or one poll interval when no reset
    time is known), bounded by the remaining --max-duration budget. Returns True
    when the loop should retry, or False when the time budget is exhausted.
    """
    target = soonest_wait_until(selection)
    now = common.now_epoch()
    if target is not None:
        wait_s: float = max(0, target - now)
        wait_msg = f"until {common.format_local_epoch(target)} (epoch {target})"
    else:
        wait_s = float(int(cfg.poll_interval))
        wait_msg = f"{int(wait_s)}s before retrying"
    if max_duration:
        remaining = max_duration - (monotonic() - start_monotonic)
        if remaining <= 0:
            return False
        wait_s = min(wait_s, remaining)
    common.log_event(logs, "all_blocked_suspend", {"reason": reason, "wait_seconds": int(wait_s), "wait_until": target})
    status_line(f"all configured providers blocked ({reason}); suspending {wait_msg}", level="warn")
    sleep_seconds(wait_s)
    return True


def rtc_suspend(logs: common.RunLogs, target_epoch: int) -> bool:
    """Suspend the computer with an RTC wake-up timer set to target_epoch.

    Returns True only if the machine was actually suspended (and has since
    resumed). Returns False — so the caller falls back to an in-process wait —
    when suspend infrastructure is missing, the lead time is too short, or
    suspension is disabled for testing/dry-run.
    """
    if os.environ.get("LLM_SCHEDULER_NO_ACTUAL_SUSPEND", "0") == "1":
        common.log_event(logs, "rtc_suspend_skipped", {"reason": "env", "target_epoch": target_epoch})
        return False
    if not (common.have_cmd("systemd-run") and common.have_cmd("systemctl")):
        common.log_event(logs, "rtc_suspend_skipped", {"reason": "missing-systemd", "target_epoch": target_epoch})
        return False
    min_lead = int(os.environ.get("LLM_SCHEDULER_SUSPEND_MIN_LEAD", "120") or "120")
    if target_epoch - common.now_epoch() < min_lead:
        common.log_event(logs, "rtc_suspend_skipped", {"reason": "insufficient-lead", "target_epoch": target_epoch, "min_lead": min_lead})
        return False
    unit = f"ralph-robin-wake-{int(time.time())}"
    # A no-op command whose only purpose is the WakeSystem=true RTC alarm; the
    # orchestrator itself resumes in-process when systemctl suspend returns.
    proc = subprocess.run(
        [
            "systemd-run", "--user", f"--unit={unit}",
            f"--on-calendar=@{target_epoch}", "--timer-property=WakeSystem=true",
            "/bin/true",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
    )
    common.log_event(logs, "rtc_suspend_schedule", {"unit": unit, "status": proc.returncode, "output": proc.stdout, "target_epoch": target_epoch})
    if proc.returncode != 0:
        return False
    active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", f"{unit}.timer"], check=False)
    if active.returncode != 0:
        common.log_event(logs, "rtc_suspend_skipped", {"reason": "timer-not-active", "unit": unit})
        return False
    common.log_event(logs, "rtc_suspend", {"unit": unit, "target_epoch": target_epoch})
    subprocess.run(["sync"], check=False)
    subprocess.run(["systemctl", "suspend"], check=False)
    return True


def suspend_machine_until(
    cfg: RalphConfig,
    logs: common.RunLogs,
    target_epoch: int,
    start_monotonic: float,
    max_duration: int,
) -> bool:
    """Suspend until target_epoch (the earliest provider renewal), then resume.

    Ralph owns this cross-provider suspend: it sleeps the whole machine via an
    RTC wake-up timer and, on resume, continues its own rotation loop. Falls back
    to an in-process wait when real suspend is unavailable. Returns True when the
    loop should continue, or False when the --max-duration budget is exhausted.
    """
    now = common.now_epoch()
    wait_s: float = max(0, target_epoch - now)
    if max_duration:
        remaining = max_duration - (monotonic() - start_monotonic)
        if remaining <= 0:
            return False
        if wait_s > remaining:
            wait_s = remaining
            target_epoch = now + int(remaining)
    common.log_event(logs, "suspend_until_renewal", {"target_epoch": target_epoch, "wait_seconds": int(wait_s)})
    status_line(
        f"all providers rate-limited; suspending until earliest renewal {common.format_local_epoch(target_epoch)} (epoch {target_epoch})",
        level="warn",
    )
    rtc_suspend(logs, target_epoch)
    # Guarantee we do not return before the renewal even if real suspend was
    # unavailable or inhibited (a no-op systemctl suspend must not busy-loop).
    remaining = target_epoch - common.now_epoch()
    if remaining > 0:
        sleep_seconds(remaining)
    return True


def main(argv: list[str] | None = None) -> int:
    common.migrate_legacy_cache_dirs()
    cfg = parse_args(list(sys.argv[1:] if argv is None else argv))
    conf = apply_config(cfg)
    validate_args(cfg)
    resolve_policies(cfg, conf)
    logs = common.setup_run_logs(cfg.log_dir, "ralph-robin")
    prompt, prompt_sha = common.load_prompt(cfg.prompt_text, cfg.prompt_file, logs)
    cfg.prompt_text = prompt
    common.log_text(logs, f"start providers={cfg.providers_spec} cwd={cfg.cwd}")
    common.log_text(logs, f"run directory: {logs.run_dir}")
    common.log_event(logs, "start", safe_args_json(cfg))
    common.log_event(logs, "prompt", {"source": cfg.prompt_source, "sha256": prompt_sha, "prompt": prompt})
    current_index = current_index_from_state(cfg)
    status_line(f"logs: {logs.run_dir}", level="dim")
    skipped: set[str] = set()
    max_iterations = int(cfg.max_iterations)
    max_duration = parse_duration(cfg.max_duration) or 0
    min_iteration_seconds = int(cfg.min_iteration_seconds)
    start_monotonic = monotonic()
    completed = 0
    fast_streak = 0
    hard_fail_streak = 0
    iteration = 0

    def out_of_time() -> bool:
        return bool(max_duration) and (monotonic() - start_monotonic) >= max_duration

    def stop_timed_out() -> int:
        # Stopping on the time budget is success when we made progress, but an
        # autonomy-abort when every provider stayed blocked the whole time.
        code = 0 if completed else common.AUTONOMY_ABORT_STATUS
        common.log_event(logs, "final", {"status": "success" if completed else "autonomy-abort", "completed": completed, "reason": "max-duration"})
        status_line(f"reached max duration after {completed} iteration(s); stopping", level="ok" if completed else "warn")
        return code

    while True:
        if out_of_time():
            return stop_timed_out()
        common.log_event(logs, "state", {"state_file": str(cfg.state_file), "current_index": current_index})
        selection = select_provider(cfg, logs, current_index, skipped)
        common.log_event(logs, "selection", {**selection, "skipped": sorted(skipped)})
        selected_index = int(selection.get("index", -1))
        selected_provider = str(selection.get("provider", ""))
        if selected_index == -1 or not selected_provider:
            # Every provider is blocked. Do not stop: wait for one to free up.
            if not suspend_until_available(cfg, logs, selection, start_monotonic, max_duration, "all-providers-skipped"):
                return stop_timed_out()
            skipped = set()
            continue
        reason = str(selection.get("rotation_reason"))
        all_rate_limited = bool(selection.get("all_rate_limited"))
        selected_model = effective_model_for(cfg, selected_provider, selection.get("decision") or {})
        provider_label = f"{selected_provider}[{selected_model}]" if selected_model else selected_provider
        common.log_text(logs, f"selected provider={selected_provider} model={selected_model or '-'} reason={reason} all_rate_limited={str(all_rate_limited).lower()}")
        print_usage_summary(selection)
        level = "warn" if all_rate_limited else "ok"
        status_line(f"selected {provider_label} ({reason})", level=level)
        if all_rate_limited:
            # Every provider is rate-limited. Ralph owns the suspend: sleep the
            # machine until the EARLIEST window renews across the rotation, then
            # resume this loop and re-evaluate which provider to use. Do not run a
            # provider or count this as an increment.
            target = soonest_wait_until(selection) or (common.now_epoch() + int(cfg.poll_interval))
            save_state(cfg, selected_index, selected_provider)
            if cfg.dry_run:
                common.log_event(logs, "final", {"status": "dry-run"})
                print("dry-run: no prompt submitted", file=sys.stderr)
                return 0
            if not suspend_machine_until(cfg, logs, target, start_monotonic, max_duration):
                return stop_timed_out()
            skipped = set()
            continue
        save_state(cfg, selected_index, selected_provider)
        if cfg.dry_run:
            common.log_event(logs, "final", {"status": "dry-run"})
            print("dry-run: no prompt submitted", file=sys.stderr)
            return 0
        provider_prompt = provider_prompt_for(cfg, selected_provider, selection, prompt)
        iteration += 1
        scfg = scheduler_config_for(cfg, selected_provider, logs, provider_prompt, iteration, model=selected_model)
        common.log_event(logs, "scheduler_command", {"argv": ["llm-scheduler", "--provider", selected_provider, *(["--model", selected_model] if selected_model else [])], "iteration": iteration, "run_dir": str(scfg.run_dir)})
        iter_start = monotonic()
        status = run_scheduler_inline(scfg)
        iter_seconds = monotonic() - iter_start
        common.log_event(logs, "scheduler_result", {"status": status, "seconds": round(iter_seconds, 3)})
        if status == 0:
            completed += 1
            hard_fail_streak = 0
            common.log_event(logs, "iteration_complete", {"provider": selected_provider, "index": selected_index, "completed": completed, "seconds": round(iter_seconds, 3)})
            if max_iterations and completed >= max_iterations:
                common.log_event(logs, "final", {"status": "success", "completed": completed})
                status_line(f"completed {completed} iteration(s); stopping (--max-iterations)", level="ok")
                return 0
            # Tight-loop guard: a real increment takes time. If the provider keeps
            # "succeeding" instantly it is not doing work; pace the loop and abort
            # a sustained instant-success streak so the orchestrator stays in
            # control instead of spinning and burning quota.
            if min_iteration_seconds > 0 and iter_seconds < min_iteration_seconds:
                fast_streak += 1
                if fast_streak >= FAST_SUCCESS_ABORT_STREAK:
                    common.log_event(logs, "final", {"status": "autonomy-abort", "reason": "fast-success-loop", "completed": completed, "streak": fast_streak})
                    status_line(f"{selected_provider} returned success in under {min_iteration_seconds}s {fast_streak} times running; aborting to stay in control", level="error")
                    return common.AUTONOMY_ABORT_STATUS
                sleep_seconds(min_iteration_seconds - iter_seconds)
            else:
                fast_streak = 0
            status_line(f"{selected_provider} finished increment {completed}; re-selecting provider", level="ok")
            # Persistent Ralph loop: hand back, re-evaluate usage, and continue.
            # Stay anchored on the provider that just ran so even-burn keeps it
            # while it remains the best choice, and current-until-exhausted keeps
            # using it until a limit is hit.
            current_index = selected_index
            skipped = set()
            continue
        if status == common.AUTONOMY_ABORT_STATUS:
            common.log_text(logs, f"scheduler autonomy-abort for provider={selected_provider}; re-evaluating rotation")
            common.log_event(logs, "provider_autonomy_abort", {"provider": selected_provider, "index": selected_index})
            status_line(f"{selected_provider} blocked autonomously; re-evaluating rotation", level="warn")
            skipped.add(selected_provider)
            if len(skipped) >= len(cfg.providers):
                # All providers aborted this pass. Do not stop: wait and retry.
                if not suspend_until_available(cfg, logs, selection, start_monotonic, max_duration, "all-providers-autonomy-abort"):
                    return stop_timed_out()
                skipped = set()
                continue
            current_index = (selected_index + 1) % len(cfg.providers)
            continue
        # A non-zero, non-abort exit is a hard provider failure (crash, broken
        # CLI, exhausted submission retries). For a persistent loop this must not
        # terminate the whole run: rotate to the next provider exactly like an
        # autonomy abort. Only a sustained failure streak with no successful
        # increment in between — a permanently broken setup — is fatal.
        hard_fail_streak += 1
        common.log_text(logs, f"scheduler hard failure for provider={selected_provider} exit={status} streak={hard_fail_streak}; re-evaluating rotation")
        common.log_event(logs, "provider_failed", {"provider": selected_provider, "index": selected_index, "exit_code": status, "streak": hard_fail_streak})
        status_line(f"{selected_provider} failed (exit {status}); re-evaluating rotation", level="error")
        if hard_fail_streak >= HARD_FAIL_ABORT_STREAK:
            common.log_event(logs, "final", {"status": "failed", "exit_code": status, "reason": "hard-fail-streak", "streak": hard_fail_streak})
            status_line(f"{hard_fail_streak} provider failures in a row with no progress; stopping", level="error")
            return status
        skipped.add(selected_provider)
        if len(skipped) >= len(cfg.providers):
            # Every provider hard-failed this pass. Do not stop: wait and retry.
            if not suspend_until_available(cfg, logs, selection, start_monotonic, max_duration, "all-providers-failed"):
                return stop_timed_out()
            skipped = set()
            continue
        current_index = (selected_index + 1) % len(cfg.providers)
        continue


if __name__ == "__main__":
    raise SystemExit(main())
