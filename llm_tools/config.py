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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import common
from .capacity import ALL_PROVIDERS


# Allowed keys per section. Unknown keys are a hard error so typos surface
# immediately instead of being silently ignored.
_TOP_LEVEL_KEYS = frozenset({"defaults", "providers", "ralph", "scheduler", "routes", "budget", "copilot"})
_BUDGET_KEYS = frozenset({"monthly", "currency"})
_COPILOT_KEYS = frozenset({"monthly_spend_limit", "currency"})
_DEFAULTS_KEYS = frozenset({"providers", "scope", "min_remaining"})
_PROVIDER_KEYS = frozenset({"model", "allow_fallback", "scope", "min_remaining", "capacity_provider"})
# Tool sections accept any key a CLI flag maps to; validation of individual
# values happens in the existing validate_args paths once they are applied.
_RALPH_KEYS = frozenset(
    {
        "providers",
        "routes",
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

# Route table. Each ``[routes.<id>]`` block has at most these top-level
# keys. ``capacity`` and ``cost`` are themselves tables with their own
# allow-lists below.
_ROUTE_KEYS = frozenset({"provider", "model", "allow_fallback", "capacity", "cost"})
_CAPACITY_POLICY_KEYS = frozenset({"policy", "scope", "label", "provider"})
_COST_POLICY_KEYS = frozenset({"policy", "amount", "currency", "period"})

# Recognised capacity / cost policy values. The actual canonical lists
# live in ``llm_tools.routes`` to keep the policy vocabulary in one
# place; ``config`` imports them for validation so a typo is caught
# at load time, not at first launch.
_CAPACITY_POLICIES: tuple[str, ...] = (
    "provider",
    "provider_model",
    "delegate",
    "opaque",
    "ungated",
    "balance",
    "budget",
)
_COST_POLICIES: tuple[str, ...] = (
    "included",
    "fixed_subscription",
    "metered_balance",
    "metered_budget",
    "free",
    "external",
    "unknown",
)


# --- Dataclasses -------------------------------------------------------------


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
    MiniMax API, or Kilo pointed at z.AI's GLM family): set
    ``capacity_provider = "minimax"`` or ``capacity_provider = "zai"`` so
    ralph/scheduler gate, rank, and suspend on that capacity source's real
    windows instead of the launch provider's own (often irrelevant) balance.
    """

    model: str | None = None
    allow_fallback: bool = False
    scope: str | None = None
    min_remaining: str | None = None
    capacity_provider: str | None = None


@dataclass
class CapacityPolicyConfig:
    """Route-level capacity policy.

    ``policy`` is one of the strings in
    :data:`llm_tools.routes.CAPACITY_POLICIES`. ``scope``, ``label``,
    and ``provider`` are policy-specific metadata:
    ``delegate`` requires ``provider``; ``opaque`` optionally
    overrides ``scope`` (default ``subscription``) and ``label``.
    """

    policy: str = "provider"
    scope: str | None = None
    label: str | None = None
    provider: str | None = None


@dataclass
class CostPolicyConfig:
    """Route-level cost policy.

    ``policy`` is one of the strings in
    :data:`llm_tools.routes.COST_POLICIES`. ``amount``, ``currency``,
    and ``period`` are display-only metadata: they NEVER influence
    readiness.
    """

    policy: str = "unknown"
    amount: float | None = None
    currency: str | None = None
    period: str | None = None


@dataclass
class RoutePolicy:
    """Resolved routing policy for a single schedulable route.

    A route is the unit Ralph Robin rotates over when one provider
    can serve several underlying models with different capacity and
    cost semantics. ``route_id`` is the stable user-facing identifier
    (the table key in ``[routes.<id>]``). ``provider`` is the launch
    CLI; ``model`` is the model pin (None means "let the provider
    pick"). ``allow_fallback`` mirrors the legacy
    :class:`ProviderPolicy` semantics. ``capacity`` and ``cost`` are
    the route-level policy objects.
    """

    route_id: str
    provider: str
    model: str | None = None
    allow_fallback: bool = False
    capacity: CapacityPolicyConfig = field(default_factory=CapacityPolicyConfig)  # type: ignore[assignment]
    cost: CostPolicyConfig = field(default_factory=CostPolicyConfig)  # type: ignore[assignment]


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
    _validate_section(raw.get("budget"), "budget", _BUDGET_KEYS)
    _validate_budget(raw.get("budget"))
    _validate_section(raw.get("copilot"), "copilot", _COPILOT_KEYS)
    _validate_copilot(raw.get("copilot"))
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
    _validate_routes(raw.get("routes"), providers or {})
    return raw


def _validate_budget(budget: Any) -> None:
    """Validate the optional ``[budget]`` table.

    ``monthly`` is the overall monthly spend ceiling that all providers' add-on
    costs are measured against; ``currency`` is its display symbol. The amount
    must be a positive number so a progress bar can be computed against it.
    """
    if budget is None:
        return
    if "monthly" in budget:
        amount = budget["monthly"]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            _fail("budget.monthly must be a number")
        if amount <= 0:
            _fail("budget.monthly must be greater than 0")
    if "currency" in budget and not isinstance(budget["currency"], str):
        _fail("budget.currency must be a string")


def monthly_budget(cfg: dict[str, Any]) -> tuple[float | None, str]:
    """Return ``(amount, currency)`` for the overall monthly budget.

    ``amount`` is ``None`` when no budget is configured. ``currency`` defaults to
    ``"$"`` so spend rows render consistently even before a budget is set.
    """
    budget = cfg.get("budget") if isinstance(cfg, dict) else None
    if not isinstance(budget, dict):
        return None, "$"
    amount = budget.get("monthly")
    currency = str(budget.get("currency") or "$")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        return None, currency
    return float(amount), currency


def _validate_copilot(copilot: Any) -> None:
    """Validate the optional ``[copilot]`` table.

    ``monthly_spend_limit`` is the pay-as-you-go ceiling for Copilot premium
    requests / AI Credits *beyond* the plan's included allowance. GitHub does
    not expose this limit through its REST API, so the user declares it here;
    the dashboard then treats Copilot as ready (overage funded) while the
    month's billed spend stays under it. The amount must be a positive number.
    """
    if copilot is None:
        return
    if "monthly_spend_limit" in copilot:
        amount = copilot["monthly_spend_limit"]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            _fail("copilot.monthly_spend_limit must be a number")
        if amount <= 0:
            _fail("copilot.monthly_spend_limit must be greater than 0")
    if "currency" in copilot and not isinstance(copilot["currency"], str):
        _fail("copilot.currency must be a string")


def copilot_spend_limit(cfg: dict[str, Any]) -> tuple[float | None, str]:
    """Return ``(limit, currency)`` for the Copilot pay-as-you-go spend limit.

    ``limit`` is ``None`` when no limit is configured (the dashboard then keeps
    the existing behaviour: Copilot is gated solely by its included allowance,
    unless GitHub billing shows overage is already being charged). ``currency``
    defaults to ``"$"``.
    """
    copilot = cfg.get("copilot") if isinstance(cfg, dict) else None
    if not isinstance(copilot, dict):
        return None, "$"
    amount = copilot.get("monthly_spend_limit")
    currency = str(copilot.get("currency") or "$")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        return None, currency
    return float(amount), currency


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


def _validate_routes(routes: Any, providers: dict[str, Any]) -> None:
    """Validate the ``[routes.<id>]`` table.

    Each route must declare a known provider. The ``capacity`` /
    ``cost`` sub-tables are validated against the canonical policy
    vocabularies in :mod:`llm_tools.routes`. A ``delegate`` capacity
    policy must name a target; self-delegation and chains are
    rejected. A conflicting legacy ``capacity_provider`` plus a route's
    ``capacity.policy = "opaque"`` (which would mean "we have truth
    AND we have no truth") is rejected.
    """
    if routes is None:
        return
    if not isinstance(routes, dict):
        _fail("'routes' must be a table of route id to policy")
    for route_id, route_block in routes.items():
        if not isinstance(route_block, dict):
            _fail(f"routes.{route_id} must be a table")
        unknown_keys = set(route_block) - _ROUTE_KEYS
        if unknown_keys:
            _fail(f"routes.{route_id}: unknown key(s): {', '.join(sorted(unknown_keys))}")
        provider = route_block.get("provider")
        if not provider:
            _fail(f"routes.{route_id}: 'provider' is required")
        if not isinstance(provider, str) or provider not in ALL_PROVIDERS:
            _fail(
                f"routes.{route_id}: unknown provider {provider!r} "
                f"(known: {', '.join(ALL_PROVIDERS)})"
            )
        model = route_block.get("model")
        if model is not None and not isinstance(model, str):
            _fail(f"routes.{route_id}.model must be a string when set")
        if "allow_fallback" in route_block and not isinstance(route_block["allow_fallback"], bool):
            _fail(f"routes.{route_id}.allow_fallback must be true or false")

        capacity_block = route_block.get("capacity") or {}
        if not isinstance(capacity_block, dict):
            _fail(f"routes.{route_id}.capacity must be a table")
        capacity_unknown = set(capacity_block) - _CAPACITY_POLICY_KEYS
        if capacity_unknown:
            _fail(
                f"routes.{route_id}.capacity: unknown key(s): {', '.join(sorted(capacity_unknown))}"
            )
        cap_policy = capacity_block.get("policy", "provider")
        if cap_policy not in _CAPACITY_POLICIES:
            _fail(
                f"routes.{route_id}.capacity.policy {cap_policy!r} is not a known "
                f"capacity policy (known: {', '.join(_CAPACITY_POLICIES)})"
            )
        if cap_policy == "delegate":
            target = capacity_block.get("provider")
            if not target:
                _fail(
                    f"routes.{route_id}.capacity.policy = 'delegate' requires "
                    f"'capacity.provider' to name a known provider"
                )
            if target not in ALL_PROVIDERS:
                _fail(
                    f"routes.{route_id}.capacity.provider {target!r} must be a known provider"
                )
            if target == provider:
                _fail(
                    f"routes.{route_id}.capacity.provider cannot reference its own provider"
                )
            target_route = routes.get(target) if isinstance(routes.get(target), dict) else None
            if target_route and isinstance(target_route.get("capacity"), dict):
                if target_route["capacity"].get("policy") == "delegate":
                    _fail(
                        f"routes.{route_id}.capacity.provider points at '{target}', "
                        f"which itself delegates; capacity links must be a single hop"
                    )

        cost_block = route_block.get("cost") or {}
        if not isinstance(cost_block, dict):
            _fail(f"routes.{route_id}.cost must be a table")
        cost_unknown = set(cost_block) - _COST_POLICY_KEYS
        if cost_unknown:
            _fail(
                f"routes.{route_id}.cost: unknown key(s): {', '.join(sorted(cost_unknown))}"
            )
        cost_policy = cost_block.get("policy", "unknown")
        if cost_policy not in _COST_POLICIES:
            _fail(
                f"routes.{route_id}.cost.policy {cost_policy!r} is not a known "
                f"cost policy (known: {', '.join(_COST_POLICIES)})"
            )

        if cap_policy == "opaque":
            legacy = providers.get(provider) or {}
            if legacy.get("capacity_provider"):
                _fail(
                    f"routes.{route_id} sets capacity.policy = 'opaque' but "
                    f"providers.{provider}.capacity_provider is also set; "
                    f"choose one source of truth"
                )


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


def route_policy(cfg: dict[str, Any], route_id: str) -> RoutePolicy | None:
    """Resolve a single :class:`RoutePolicy` from a loaded config dict.

    Returns ``None`` when ``route_id`` is not declared under
    ``[routes.<id>]``. Callers that require a route should treat
    ``None`` as a configuration error and exit.
    """
    if not cfg:
        return None
    routes = cfg.get("routes") or {}
    block = routes.get(route_id) if isinstance(routes, dict) else None
    if not isinstance(block, dict):
        return None
    return _route_from_block(str(route_id), block)


def parse_routes(cfg: dict[str, Any]) -> dict[str, RoutePolicy]:
    """Parse every ``[routes.<id>]`` entry into :class:`RoutePolicy`.

    The result is keyed by ``route_id`` and preserves declaration order
    when iterated (Python 3.7+). The parser is silent about order: it
    validates every entry, then returns. Unknown / invalid routes are
    rejected at :func:`load_config` time.
    """
    if not cfg:
        return {}
    raw = cfg.get("routes") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, RoutePolicy] = {}
    for route_id, block in raw.items():
        if not isinstance(block, dict):
            continue
        out[str(route_id)] = _route_from_block(str(route_id), block)
    return out


def _route_from_block(route_id: str, block: dict[str, Any]) -> RoutePolicy:
    provider = str(block.get("provider") or "")
    model = block.get("model")
    capacity_block = block.get("capacity") or {}
    cost_block = block.get("cost") or {}
    capacity = CapacityPolicyConfig(
        policy=str(capacity_block.get("policy", "provider")),
        scope=_as_str(capacity_block.get("scope")),
        label=_as_str(capacity_block.get("label")),
        provider=_as_str(capacity_block.get("provider")),
    )
    cost_policy = str(cost_block.get("policy", "unknown"))
    amount_raw = cost_block.get("amount")
    amount: float | None
    if amount_raw is None or amount_raw == "":
        amount = None
    elif isinstance(amount_raw, bool):
        amount = None
    elif isinstance(amount_raw, (int, float)):
        amount = float(amount_raw)
    else:
        try:
            amount = float(str(amount_raw))
        except ValueError:
            amount = None
    cost = CostPolicyConfig(
        policy=cost_policy,
        amount=amount,
        currency=_as_str(cost_block.get("currency")),
        period=_as_str(cost_block.get("period")),
    )
    return RoutePolicy(
        route_id=route_id,
        provider=provider,
        model=str(model) if model is not None else None,
        allow_fallback=bool(block.get("allow_fallback", False)),
        capacity=capacity,
        cost=cost,
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
    "CapacityPolicyConfig",
    "CostPolicyConfig",
    "ProviderPolicy",
    "RoutePolicy",
    "config_path",
    "load_config",
    "merged_tool_config",
    "parse_routes",
    "provider_policy",
    "route_policy",
]
