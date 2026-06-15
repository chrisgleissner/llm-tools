"""Route-level capacity and cost policy for llm-tools.

A **route** is a schedulable launch path: a CLI provider, a model, a
capacity policy (how readiness is decided), and a cost policy (how the
entitlement is displayed). It is the unit Ralph Robin rotates over when
one provider can serve several underlying models with different
capacity and cost semantics.

The route layer sits *above* the existing provider readers
(``read_kilo`` / ``read_opencode`` / ...) and the
:func:`llm_tools.capacity.decide` decider. It does not replace them;
the existing ``usage_snapshot_and_decision`` path is the
provider-decision helper, and the new
:func:`usage_snapshot_and_decision_for_route` is the
route-decision helper. They share the same public JSON shape so a
caller that already knew ``provider / usable / reason / wait_until /
windows / exhausted`` keeps working when a route is supplied instead
of a provider.

Vocabulary
----------

Capacity policy
    How readiness is decided for this route. One of:

    * ``provider``        — use the provider's aggregate capacity reader.
    * ``provider_model``  — same as ``provider`` today (model-specific
      sub-buckets are read transparently; this policy exists so a
      route can document that it is intentionally model-aware).
    * ``delegate``        — launch this route's provider, but read
      capacity from another provider. The route-level successor to
      ``capacity_provider``.
    * ``opaque``          — the route is usable when the launch CLI is
      available and no local runtime block exists. Remaining capacity
      is unknown before launch.
    * ``ungated``         — the route is usable when the launch CLI is
      available. Not rankable.
    * ``balance``         — read the provider's ``balance`` scope.
    * ``budget``          — read the provider's ``budget`` scope.

Cost policy
    How the entitlement is displayed and attributed. It NEVER
    influences readiness. One of: ``included``,
    ``fixed_subscription``, ``metered_balance``, ``metered_budget``,
    ``free``, ``external``, ``unknown``.

Scope name
    A displayed / gateable capacity dimension. The new ``subscription``
    scope is the canonical way to model opaque prepaid entitlements.

Kind
    A capacity kind. The new ``opaque`` kind describes capacity that
    exists but cannot be measured before launch.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import common
from .capacity import (
    ALL_PROVIDERS,
    CapacityKind,
    CapacityScope,
    ProviderSnapshot,
    SCOPE_BALANCE,
    SCOPE_BUDGET,
    SCOPE_SUBSCRIPTION,
    SCOPE_UNGATED,
    UsageDecision,
    decide,
)
from .config import (
    CapacityPolicyConfig,
    CostPolicyConfig,
    RoutePolicy,
    parse_routes,
    route_policy,
)


# Canonical policy names. Listed explicitly so config validation can
# hard-fail on typos before any reader ever runs.

CAPACITY_POLICY_PROVIDER = "provider"
CAPACITY_POLICY_PROVIDER_MODEL = "provider_model"
CAPACITY_POLICY_DELEGATE = "delegate"
CAPACITY_POLICY_OPAQUE = "opaque"
CAPACITY_POLICY_UNGATED = "ungated"
CAPACITY_POLICY_BALANCE = "balance"
CAPACITY_POLICY_BUDGET = "budget"

CAPACITY_POLICIES: tuple[str, ...] = (
    CAPACITY_POLICY_PROVIDER,
    CAPACITY_POLICY_PROVIDER_MODEL,
    CAPACITY_POLICY_DELEGATE,
    CAPACITY_POLICY_OPAQUE,
    CAPACITY_POLICY_UNGATED,
    CAPACITY_POLICY_BALANCE,
    CAPACITY_POLICY_BUDGET,
)

COST_POLICY_INCLUDED = "included"
COST_POLICY_FIXED_SUBSCRIPTION = "fixed_subscription"
COST_POLICY_METERED_BALANCE = "metered_balance"
COST_POLICY_METERED_BUDGET = "metered_budget"
COST_POLICY_FREE = "free"
COST_POLICY_EXTERNAL = "external"
COST_POLICY_UNKNOWN = "unknown"

COST_POLICIES: tuple[str, ...] = (
    COST_POLICY_INCLUDED,
    COST_POLICY_FIXED_SUBSCRIPTION,
    COST_POLICY_METERED_BALANCE,
    COST_POLICY_METERED_BUDGET,
    COST_POLICY_FREE,
    COST_POLICY_EXTERNAL,
    COST_POLICY_UNKNOWN,
)


# --- Local runtime block ledger ----------------------------------------------
#
# Opaque routes cannot be percent-gated. When the launch CLI returns a
# real runtime quota / plan / credit failure we record a local block so
# the route stops being selected until the retry timestamp expires (or
# a successful run clears the block). The ledger is one small JSON
# file per route under the llm-tools cache root. It is intentionally
# hermetic: missing / corrupt files are dropped, never raised.

DEFAULT_LOCAL_BLOCK_BACKOFF_SECONDS = 300


def local_block_dir(env: dict[str, str] | None = None) -> Path:
    """Directory that stores per-route local block files.

    Override via ``LLM_TOOLS_LOCAL_BLOCK_DIR`` (mainly for tests). The
    default is :func:`llm_tools.common.cache_root` / "routes" / "blocks".
    """
    env = env or os.environ
    override = env.get("LLM_TOOLS_LOCAL_BLOCK_DIR")
    if override:
        return Path(override)
    return common.cache_root(env) / "routes" / "blocks"


def _route_block_path(route_id: str, env: dict[str, str] | None = None) -> Path:
    safe = "".join(c for c in route_id if c.isalnum() or c in ("-", "_", ".")) or "route"
    return local_block_dir(env) / f"{safe}.json"


def read_local_block(route_id: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Return the current block for ``route_id`` or ``None``.

    A block is ``{"route_id", "reason", "blocked_until", "last_message",
    "last_seen", "backoff_seconds"}``. A corrupt / missing / non-dict
    file is treated as "no block" and silently dropped, so a one-off
    bad write does not break the orchestrator.
    """
    path = _route_block_path(route_id, env)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def is_locally_blocked(route_id: str, env: dict[str, str] | None = None) -> bool:
    return read_local_block(route_id, env) is not None


def record_local_block(
    route_id: str,
    *,
    reason: str,
    blocked_until: int,
    last_message: str = "",
    backoff_seconds: int = 0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Persist a local block for ``route_id``.

    Returns the persisted block. Missing parent directories are created
    with ``0o700`` perms; the file is written ``0o600``.
    """
    now = common.now_epoch()
    payload: dict[str, Any] = {
        "route_id": route_id,
        "reason": str(reason or "blocked")[:64],
        "blocked_until": int(blocked_until),
        "last_message": str(last_message)[:2000],
        "last_seen": int(now),
        "backoff_seconds": int(backoff_seconds or 0),
    }
    path = _route_block_path(route_id, env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        # Recording the block is best-effort. The orchestrator should never
        # crash on a permission error in the cache root.
        pass
    return payload


def clear_local_block(route_id: str, env: dict[str, str] | None = None) -> bool:
    """Remove the local block for ``route_id``.

    Returns ``True`` when a block existed and was removed, ``False``
    when there was nothing to clear (including corrupt / missing
    files).
    """
    path = _route_block_path(route_id, env)
    if not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def default_backoff_seconds() -> int:
    raw = os.environ.get("LLM_TOOLS_LOCAL_BLOCK_BACKOFF", str(DEFAULT_LOCAL_BLOCK_BACKOFF_SECONDS))
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_LOCAL_BLOCK_BACKOFF_SECONDS
    return max(5, value)


# --- Opaque scope construction -----------------------------------------------


def opaque_scope_for_route(
    route: RoutePolicy,
    *,
    cli_present: bool,
    blocked: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> CapacityScope:
    """Build the canonical :class:`CapacityScope` for an opaque route.

    The shape matches what the rest of the capacity model already
    understands (``name``, ``kind``, ``ready``, ``label``, ``source``,
    ``extras``). An opaque scope is always ready when the launch CLI
    is present and there is no local block; it is never percent-gated
    so its ``remaining_percent`` / ``reset_epoch`` stay ``None``.
    """
    env = env or os.environ
    now = common.now_epoch()
    if blocked is not None:
        blocked_until = int(blocked.get("blocked_until", now))
        reason = str(blocked.get("reason", "blocked"))
        ready = False
    else:
        blocked_until = None
        reason = ""
        ready = bool(cli_present)
    label = route.capacity.label or route.route_id
    scope_name = route.capacity.scope or SCOPE_SUBSCRIPTION
    source = f"config:route:{route.route_id}"
    return CapacityScope(
        name=scope_name,
        kind=CapacityKind.OPAQUE,
        ready=ready,
        reason=reason if not ready else "",
        remaining_percent=None,
        reset_epoch=blocked_until,
        resets_at=None,
        remaining_amount=None,
        total_amount=None,
        currency=route.cost.currency,
        label=label,
        source=source,
        extras={
            "route_id": route.route_id,
            "provider": route.provider,
            "model": route.model or "",
            "cost_policy": route.cost.policy,
            "cost_amount": route.cost.amount,
            "cost_currency": route.cost.currency,
            "cost_period": route.cost.period,
            "blocked_reason": reason,
        },
    )


# --- Provider launch CLI presence --------------------------------------------


def route_cli_present(route: RoutePolicy, env: dict[str, str] | None = None) -> bool:
    """Whether the route's launch CLI is reachable on ``env['PATH']``."""
    env = env or os.environ
    import shutil

    return shutil.which(route.provider, path=env.get("PATH")) is not None


# --- Route decision helper ---------------------------------------------------


def _legacy_windows_from_scopes(scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _windows_from_capacity_scopes(scopes: list[CapacityScope]) -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "kind": s.kind,
            "remaining": s.remaining_percent,
            "remaining_amount": s.remaining_amount,
            "currency": s.currency,
            "resets_at": s.resets_at,
            "reset_epoch": s.reset_epoch,
            "source": s.source,
        }
        for s in scopes
    ]


def usage_snapshot_and_decision_for_route(
    route: RoutePolicy,
    scope: str,
    min_remaining: str,
    poll_interval: str,
    env: dict[str, Any] | None = None,
    *,
    model: str | None = None,
    allow_fallback: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Snapshot + decision for a :class:`RoutePolicy`.

    Returns ``(snapshot, decision)`` shaped like the existing public
    JSON: ``snapshot`` carries ``route``, ``provider``, ``selected_model``,
    ``available``, ``reason``, ``source``, ``scopes`` (a list of
    plain dicts), and ``cost``; ``decision`` carries ``provider``,
    ``usable``, ``reason``, ``wait_until``, ``windows``, and
    ``exhausted``.
    """
    env = env or os.environ
    policy = route.capacity.policy
    snapshot: dict[str, Any]
    decision: dict[str, Any]
    min_percent = float(min_remaining)
    min_amount = float(min_remaining)
    poll = max(1, int(poll_interval))

    if policy == CAPACITY_POLICY_OPAQUE:
        cli_present = route_cli_present(route, env)
        blocked = read_local_block(route.route_id, env)
        if blocked is not None:
            usable = False
            reason = "blocked"
            wait_until = int(blocked.get("blocked_until", common.now_epoch() + poll))
            cap_scope = opaque_scope_for_route(route, cli_present=cli_present, blocked=blocked, env=env)
        elif not cli_present:
            usable = False
            reason = "missing-cli"
            wait_until = common.now_epoch() + poll
            cap_scope = opaque_scope_for_route(route, cli_present=False, env=env)
        else:
            usable = True
            reason = "usable"
            wait_until = None
            cap_scope = opaque_scope_for_route(route, cli_present=True, env=env)
        cap_scope.ready = usable
        if not usable:
            cap_scope.reason = reason
        snapshot = {
            "route": route.route_id,
            "provider": route.provider,
            "selected_model": route.model or model,
            "available": usable,
            "reason": reason,
            "source": cap_scope.source,
            "scopes": [asdict(cap_scope)],
            "cost": asdict(route.cost),
        }
        decision = {
            "provider": route.provider,
            "usable": usable,
            "reason": reason,
            "wait_until": wait_until,
            "windows": _windows_from_capacity_scopes([cap_scope]),
        }
        if not usable:
            decision["exhausted"] = [
                {
                    "name": cap_scope.name,
                    "kind": cap_scope.kind,
                    "remaining": cap_scope.remaining_percent,
                    "remaining_amount": cap_scope.remaining_amount,
                    "reset_epoch": cap_scope.reset_epoch,
                }
            ]
        return snapshot, decision

    if policy in (CAPACITY_POLICY_PROVIDER, CAPACITY_POLICY_PROVIDER_MODEL, CAPACITY_POLICY_BALANCE, CAPACITY_POLICY_BUDGET, CAPACITY_POLICY_UNGATED):
        requested = scope
        if policy == CAPACITY_POLICY_BALANCE:
            requested = SCOPE_BALANCE
        elif policy == CAPACITY_POLICY_BUDGET:
            requested = SCOPE_BUDGET
        elif policy == CAPACITY_POLICY_UNGATED:
            requested = SCOPE_UNGATED
        snapshot, decision = common.usage_snapshot_and_decision(
            route.provider,
            None,
            requested,
            min_remaining,
            poll_interval,
            env,
            model=route.model or model,
            allow_fallback=allow_fallback and route.allow_fallback,
        )
        snapshot = dict(snapshot)
        snapshot["route"] = route.route_id
        snapshot["selected_model"] = route.model or model or snapshot.get("selected_model")
        snapshot["cost"] = asdict(route.cost)
        return snapshot, decision

    if policy == CAPACITY_POLICY_DELEGATE:
        target = route.capacity.provider
        if not target:
            snapshot = {
                "route": route.route_id,
                "provider": route.provider,
                "selected_model": route.model or model,
                "available": False,
                "reason": "missing-delegate",
                "source": f"config:route:{route.route_id}",
                "scopes": [],
                "cost": asdict(route.cost),
            }
            decision = {
                "provider": route.provider,
                "usable": False,
                "reason": "missing-delegate",
                "wait_until": common.now_epoch() + poll,
                "windows": [],
            }
            return snapshot, decision
        snap, dec = common.usage_snapshot_and_decision(
            target,
            None,
            scope,
            min_remaining,
            poll_interval,
            env,
            model=None,
            allow_fallback=True,
        )
        snapshot = dict(snap)
        snapshot["route"] = route.route_id
        snapshot["provider"] = route.provider
        snapshot["selected_model"] = route.model or model or snapshot.get("selected_model")
        snapshot["cost"] = asdict(route.cost)
        decision = dict(dec)
        decision["provider"] = route.provider
        decision["capacity_provider"] = target
        return snapshot, decision

    # Unknown policies should have been rejected at config parse time.
    snapshot = {
        "route": route.route_id,
        "provider": route.provider,
        "selected_model": route.model or model,
        "available": False,
        "reason": f"unsupported-policy:{policy}",
        "source": f"config:route:{route.route_id}",
        "scopes": [],
        "cost": asdict(route.cost),
    }
    decision = {
        "provider": route.provider,
        "usable": False,
        "reason": f"unsupported-policy:{policy}",
        "wait_until": common.now_epoch() + poll,
        "windows": [],
    }
    return snapshot, decision


# --- Resolution: explicit vs implicit routes ---------------------------------


def resolve_routes(cfg: dict[str, Any] | None) -> list[RoutePolicy]:
    """Resolve the route rotation for a loaded config dict.

    * When ``[ralph].routes`` is set, return only those routes in
      declared order, validated against the route table.
    * When it is absent, build one implicit route per provider in
      ``[ralph].providers`` (or ``[defaults].providers``), translating
      each :class:`ProviderPolicy` into a :class:`RoutePolicy` with
      a matching capacity policy.

    The function is pure; the caller is responsible for loading the
    config (e.g. ``toolconfig.load_config(env)``).
    """
    if not cfg:
        return []
    ralph = cfg.get("ralph") or {}
    raw_routes = ralph.get("routes")
    if isinstance(raw_routes, list) and raw_routes:
        routes: list[RoutePolicy] = []
        for entry in raw_routes:
            if not isinstance(entry, str):
                continue
            route = route_policy(cfg, entry)
            if route is not None:
                routes.append(route)
        return routes

    providers: list[str] = []
    for source in (ralph.get("providers"), (cfg.get("defaults") or {}).get("providers")):
        if isinstance(source, list):
            for item in source:
                if isinstance(item, str) and item and item not in providers:
                    providers.append(item)
    routes = []
    for provider in providers:
        if provider not in ALL_PROVIDERS:
            continue
        routes.append(route_for_provider(cfg, provider))
    return routes


def route_for_provider(cfg: dict[str, Any], provider: str) -> RoutePolicy:
    """Build a :class:`RoutePolicy` from a legacy :class:`ProviderPolicy`.

    The route is named after the provider (so the existing
    ``[providers.*]`` configs work without renaming). A
    ``capacity_provider`` is translated into a ``delegate`` capacity
    policy; otherwise the route is ``provider``.
    """
    from . import config as toolconfig

    policy = toolconfig.provider_policy(cfg, provider)
    if policy.capacity_provider:
        capacity = CapacityPolicyConfig(
            policy=CAPACITY_POLICY_DELEGATE,
            provider=policy.capacity_provider,
        )
    else:
        capacity = CapacityPolicyConfig(policy=CAPACITY_POLICY_PROVIDER)
    cost = CostPolicyConfig(policy=COST_POLICY_UNKNOWN)
    return RoutePolicy(
        route_id=provider,
        provider=provider,
        model=policy.model,
        allow_fallback=policy.allow_fallback,
        capacity=capacity,
        cost=cost,
    )


# --- JSON projection ---------------------------------------------------------


def route_to_json(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project a route snapshot into a stable JSON-friendly dict.

    Preserves the existing per-scope field set so consumers that
    already know the ``CapacityScope`` shape keep working. ``route``,
    ``selected_model``, and ``cost`` are added without removing any
    existing fields.
    """
    out: dict[str, Any] = {
        "route": snapshot.get("route"),
        "provider": snapshot.get("provider"),
        "selected_model": snapshot.get("selected_model"),
        "available": snapshot.get("available"),
        "reason": snapshot.get("reason"),
        "source": snapshot.get("source"),
        "scopes": [],
        "cost": snapshot.get("cost") or {},
    }
    for scope in snapshot.get("scopes") or []:
        if not isinstance(scope, dict):
            continue
        out["scopes"].append(
            {
                "name": scope.get("name"),
                "kind": scope.get("kind"),
                "ready": scope.get("ready", True),
                "reason": scope.get("reason", ""),
                "remaining_percent": scope.get("remaining_percent"),
                "remaining_amount": scope.get("remaining_amount"),
                "total_amount": scope.get("total_amount"),
                "currency": scope.get("currency"),
                "reset_epoch": scope.get("reset_epoch"),
                "resets_at": scope.get("resets_at"),
                "label": scope.get("label"),
                "source": scope.get("source"),
                "extras": dict(scope.get("extras") or {}),
            }
        )
    return out


__all__ = [
    "CAPACITY_POLICIES",
    "CAPACITY_POLICY_BALANCE",
    "CAPACITY_POLICY_BUDGET",
    "CAPACITY_POLICY_DELEGATE",
    "CAPACITY_POLICY_OPAQUE",
    "CAPACITY_POLICY_PROVIDER",
    "CAPACITY_POLICY_PROVIDER_MODEL",
    "CAPACITY_POLICY_UNGATED",
    "COST_POLICIES",
    "COST_POLICY_EXTERNAL",
    "COST_POLICY_FIXED_SUBSCRIPTION",
    "COST_POLICY_FREE",
    "COST_POLICY_INCLUDED",
    "COST_POLICY_METERED_BALANCE",
    "COST_POLICY_METERED_BUDGET",
    "COST_POLICY_UNKNOWN",
    "DEFAULT_LOCAL_BLOCK_BACKOFF_SECONDS",
    "clear_local_block",
    "default_backoff_seconds",
    "is_locally_blocked",
    "local_block_dir",
    "opaque_scope_for_route",
    "parse_routes",
    "read_local_block",
    "record_local_block",
    "resolve_routes",
    "route_cli_present",
    "route_for_provider",
    "route_policy",
    "route_to_json",
    "usage_snapshot_and_decision_for_route",
]


# Re-export dataclasses / parser from ``config`` so the public surface
# of the routes module is self-contained.
from .config import (  # noqa: E402  (import after __all__ for clarity)
    CapacityPolicyConfig as CapacityPolicyConfig,
)
from .config import (  # noqa: E402
    CostPolicyConfig as CostPolicyConfig,
)
from .config import (  # noqa: E402
    RoutePolicy as RoutePolicy,
)
