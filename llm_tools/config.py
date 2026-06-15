"""Shared llm-tools configuration file.

A single TOML file lets ``llm-usage``, ``llm-scheduler``, and ``ralph-robin``
share user preferences instead of every behaviour being a CLI flag or
``LLM_*`` env var. The most important thing it expresses is a per-provider
*routing policy*: which model a provider should run, and whether falling back
to another model on that provider is allowed when the pinned model's own rate
limit is exhausted (disabled by default).

TOML is parsed with the standard-library ``tomllib`` (Python >= 3.11), so the
config adds no third-party dependency.

Location (first match wins):

1. ``$LLM_TOOLS_CONFIG`` (explicit path).
2. ``$XDG_CONFIG_HOME/llm-tools/config.toml``.
3. ``~/.config/llm-tools/config.toml``.

Precedence everywhere is: built-in defaults < config file < CLI flags. A
missing file means today's behaviour is unchanged.

Schema::

    [defaults]
    providers = ["claude", "codex"]   # ralph rotation default
    scope = "auto"
    min_remaining = 1

    [providers.claude]
    model = "sonnet"             # run `claude --model sonnet`; gate on Sonnet's limit
    allow_fallback = false       # Sonnet exhausted -> skip claude, do not downgrade

    [providers.codex]
    model = "spark"
    allow_fallback = false

    [ralph]                      # ralph-robin-only overrides
    even_burn = true
    max_duration = "24h"

    [scheduler]                  # llm-scheduler-only overrides
    poll_interval = 60
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import common
from .capacity import ALL_PROVIDERS


# Allowed keys per section. Unknown keys are a hard error so typos surface
# immediately instead of being silently ignored.
_TOP_LEVEL_KEYS = frozenset({"defaults", "providers", "ralph", "scheduler"})
_DEFAULTS_KEYS = frozenset({"providers", "scope", "min_remaining"})
_PROVIDER_KEYS = frozenset({"model", "allow_fallback", "scope", "min_remaining", "capacity_provider"})
# Tool sections accept any key a CLI flag maps to; validation of individual
# values happens in the existing validate_args paths once they are applied.
_RALPH_KEYS = frozenset(
    {
        "providers",
        "scope",
        "min_remaining",
        "poll_interval",
        "max_unavailable_wait",
        "retry_delays",
        "even_burn",
        "max_iterations",
        "max_duration",
        "min_iteration_seconds",
        "prefix",
        "prefix_usage_interval",
    }
)
_SCHEDULER_KEYS = frozenset(
    {
        "provider",
        "scope",
        "min_remaining",
        "poll_interval",
        "max_unavailable_wait",
        "retry_delays",
    }
)


@dataclass
class ProviderPolicy:
    """Resolved routing policy for a single provider.

    ``model`` pins the model the provider CLI runs (and the rate-limit bucket
    ralph/scheduler gate on). ``allow_fallback`` controls what happens when that
    model's own limit is exhausted: ``False`` (default) treats the provider as
    unusable so callers rotate away; ``True`` lets the provider stay usable via
    its aggregate window with the model pin dropped.

    ``capacity_provider`` ties this provider's *availability and capacity* to a
    different provider's usage windows (5h / weekly / monthly / balance / …)
    while still launching this provider's own CLI. Use it when the CLI is
    configured to run another provider's model (e.g. OpenCode pointed at the
    MiniMax API): set ``capacity_provider = "minimax"`` so ralph/scheduler gate,
    rank, and suspend on MiniMax's real windows instead of OpenCode's own
    (often irrelevant) balance.
    """

    model: str | None = None
    allow_fallback: bool = False
    scope: str | None = None
    min_remaining: str | None = None
    capacity_provider: str | None = None


def config_path(env: dict[str, str] | None = None) -> Path:
    env = env or os.environ
    explicit = env.get("LLM_TOOLS_CONFIG")
    if explicit:
        return Path(explicit)
    base = env.get("XDG_CONFIG_HOME") or str(common.home_dir(env) / ".config")
    return Path(base) / "llm-tools" / "config.toml"


# Cache parsed config by (resolved path, mtime_ns) so repeated loads within a
# run are free but an edited file is picked up on the next process.
_cache: dict[tuple[str, int], dict[str, Any]] = {}


def _fail(message: str) -> None:
    common.err(f"config: {message} (in {config_path()})")
    raise SystemExit(2)


def load_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load and validate the TOML config, or ``{}`` when no file exists."""
    env = env or os.environ
    path = config_path(env)
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        _fail(f"could not parse TOML: {exc}")
    parsed = _validate(raw)
    _cache[key] = parsed
    return parsed


def _validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _fail("top level must be a table")
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        _fail(f"unknown section(s): {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(_TOP_LEVEL_KEYS))})")
    _validate_section(raw.get("defaults"), "defaults", _DEFAULTS_KEYS)
    _validate_section(raw.get("ralph"), "ralph", _RALPH_KEYS)
    _validate_section(raw.get("scheduler"), "scheduler", _SCHEDULER_KEYS)
    providers = raw.get("providers")
    if providers is not None:
        if not isinstance(providers, dict):
            _fail("'providers' must be a table of provider name to policy")
        for name, policy in providers.items():
            if name not in ALL_PROVIDERS:
                _fail(f"unknown provider '{name}' (known: {', '.join(ALL_PROVIDERS)})")
            if not isinstance(policy, dict):
                _fail(f"providers.{name} must be a table")
            unknown_keys = set(policy) - _PROVIDER_KEYS
            if unknown_keys:
                _fail(f"providers.{name}: unknown key(s): {', '.join(sorted(unknown_keys))}")
            if "allow_fallback" in policy and not isinstance(policy["allow_fallback"], bool):
                _fail(f"providers.{name}.allow_fallback must be true or false")
        _validate_capacity_providers(providers)
    return raw


def _validate_capacity_providers(providers: dict[str, Any]) -> None:
    """Validate every ``capacity_provider`` reference in the providers table.

    A reference must name a known provider, must not point at itself, and must
    not point at a provider that itself delegates (capacity links are one hop,
    never chained), so the resolved capacity source is always unambiguous.
    """
    for name, policy in providers.items():
        target = policy.get("capacity_provider")
        if target is None:
            continue
        if not isinstance(target, str) or target not in ALL_PROVIDERS:
            _fail(f"providers.{name}.capacity_provider must be a known provider (known: {', '.join(ALL_PROVIDERS)})")
        if target == name:
            _fail(f"providers.{name}.capacity_provider cannot reference itself")
        target_policy = providers.get(target) or {}
        if target_policy.get("capacity_provider") is not None:
            _fail(
                f"providers.{name}.capacity_provider points at '{target}', which itself "
                f"sets capacity_provider; capacity links must be a single hop"
            )


def _validate_section(section: Any, name: str, allowed: frozenset[str]) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        _fail(f"'{name}' must be a table")
    unknown = set(section) - allowed
    if unknown:
        _fail(f"{name}: unknown key(s): {', '.join(sorted(unknown))} (allowed: {', '.join(sorted(allowed))})")


def provider_policy(cfg: dict[str, Any], provider: str) -> ProviderPolicy:
    """Resolve the routing policy for ``provider`` from a loaded config dict."""
    block = (cfg.get("providers") or {}).get(provider) or {}
    model = block.get("model")
    scope = block.get("scope")
    capacity_provider = block.get("capacity_provider")
    return ProviderPolicy(
        model=str(model) if model is not None else None,
        allow_fallback=bool(block.get("allow_fallback", False)),
        scope=str(scope) if scope is not None else None,
        min_remaining=_as_str(block.get("min_remaining")),
        capacity_provider=str(capacity_provider) if capacity_provider is not None else None,
    )


def merged_tool_config(cfg: dict[str, Any], tool: str) -> dict[str, Any]:
    """Merge the shared ``defaults`` block with a tool-specific block.

    The tool block (``ralph`` / ``scheduler``) wins over ``defaults`` for any
    key both set. Returns a plain dict the caller maps onto its own config
    fields for any flag the user did not pass explicitly.
    """
    merged: dict[str, Any] = {}
    merged.update(cfg.get("defaults") or {})
    merged.update(cfg.get(tool) or {})
    return merged


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


__all__ = [
    "ProviderPolicy",
    "config_path",
    "load_config",
    "merged_tool_config",
    "provider_policy",
]
