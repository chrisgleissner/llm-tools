"""Generic capacity-scope abstraction for llm-tools providers.

The legacy code modeled every provider as a handful of named rate-limit
``windows`` (5h, weekly, monthly). That was too narrow to fit Kilo Code CLI,
whose governing constraints are funded balance, an optional monthly budget,
or a BYOK/local/ungated mode where there is no quota at all. This module
introduces a single, generic ``CapacityScope`` and ``ProviderSnapshot`` model
that lets every provider describe its capacity through a small, well-typed
vocabulary, and lets generic scheduler / rotation logic reason about them
without hard-coding any provider name.

The vocabulary is intentionally small:

``CapacityKind``:
    * ``reset_window`` - a quota that resets at a known epoch
      (5h, weekly, monthly). Tracks a percentage remaining and a reset time.
    * ``balance`` - a funded amount (Kilo credits / balance). No natural
      reset; tracks a remaining amount in a currency or unit.
    * ``budget`` - an optional spend budget with a reset, used for pacing
      (Kilo monthly budget). Tracks a percentage remaining and a reset.
    * ``ungated`` - provider is usable but unmetered (BYOK / local /
      Kilo `ungated`). Tracks no numbers; the only gate is "CLI present".
    * ``unknown`` - the source could not determine a usable capacity. Used
      when the provider has not been configured or the data is inconclusive.

A ``ProviderSnapshot`` is a normalized view of a provider's capacity
independent of how that data was sourced. A ``UsageDecision`` is the answer
to "is this provider usable right now, and if not, when should we check
again?". Provider-specific readers build snapshots; generic scheduler /
rotation code consumes decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


# --- Provider identifiers -----------------------------------------------------

PROVIDER_CODEX = "codex"
PROVIDER_CLAUDE = "claude"
PROVIDER_COPILOT = "copilot"
PROVIDER_KILO = "kilo"
PROVIDER_OPENCODE = "opencode"
PROVIDER_MINIMAX = "minimax"

ALL_PROVIDERS: tuple[str, ...] = (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_COPILOT,
    PROVIDER_KILO,
    PROVIDER_MINIMAX,
    PROVIDER_OPENCODE,
)


# --- Scope identifiers --------------------------------------------------------

SCOPE_AUTO = "auto"
SCOPE_5H = "5h"
SCOPE_WEEKLY = "weekly"
SCOPE_MONTHLY = "monthly"
SCOPE_BALANCE = "balance"
SCOPE_BUDGET = "budget"
SCOPE_BYOK = "byok"
SCOPE_UNGATED = "ungated"
SCOPE_SUBSCRIPTION = "subscription"

ALL_SCOPES: tuple[str, ...] = (
    SCOPE_AUTO,
    SCOPE_5H,
    SCOPE_WEEKLY,
    SCOPE_MONTHLY,
    SCOPE_BALANCE,
    SCOPE_BUDGET,
    SCOPE_BYOK,
    SCOPE_UNGATED,
    SCOPE_SUBSCRIPTION,
)

# Scopes each provider natively supports (plus `auto`).
PROVIDER_SCOPES: dict[str, frozenset[str]] = {
    PROVIDER_CODEX: frozenset({SCOPE_AUTO, SCOPE_5H, SCOPE_WEEKLY}),
    PROVIDER_CLAUDE: frozenset({SCOPE_AUTO, SCOPE_5H, SCOPE_WEEKLY}),
    PROVIDER_COPILOT: frozenset({SCOPE_AUTO, SCOPE_MONTHLY}),
    PROVIDER_KILO: frozenset({SCOPE_AUTO, SCOPE_BALANCE, SCOPE_BUDGET, SCOPE_BYOK, SCOPE_UNGATED}),
    PROVIDER_OPENCODE: frozenset({SCOPE_AUTO, SCOPE_BALANCE, SCOPE_BUDGET, SCOPE_BYOK, SCOPE_UNGATED}),
    PROVIDER_MINIMAX: frozenset({SCOPE_AUTO, SCOPE_5H, SCOPE_WEEKLY}),
}


# --- Capacity kind ------------------------------------------------------------


class CapacityKind:
    RESET_WINDOW = "reset_window"
    BALANCE = "balance"
    BUDGET = "budget"
    UNGATED = "ungated"
    UNKNOWN = "unknown"
    OPAQUE = "opaque"


ALL_KINDS: tuple[str, ...] = (
    CapacityKind.RESET_WINDOW,
    CapacityKind.BALANCE,
    CapacityKind.BUDGET,
    CapacityKind.UNGATED,
    CapacityKind.UNKNOWN,
    CapacityKind.OPAQUE,
)


# --- Generic scope/snapshot/decision dataclasses ------------------------------


@dataclass
class CapacityScope:
    """A single capacity constraint for a provider.

    A scope is provider-agnostic: the scheduler/rotation code reasons about
    ``kind``, ``ready``, ``wait_until``, and (when present) ``remaining_percent``
    / ``remaining_amount`` without ever looking at the scope ``name``. The
    ``name`` is for display and the ``source`` is the data origin for the UI.
    """

    name: str
    kind: str
    ready: bool = True
    reason: str = ""
    # Reset-bound (reset_window) and budget scopes use percent + reset.
    remaining_percent: float | None = None
    reset_epoch: int | None = None
    resets_at: Any = None
    # Balance / budget denominators (optional).
    remaining_amount: float | None = None
    total_amount: float | None = None
    currency: str | None = None
    # Ungated scope description.
    label: str | None = None
    # Source label for the UI (e.g. "kilo stats", "$COPILOT_HOME").
    source: str = ""
    # Free-form extra fields specific to a scope kind (e.g. CLI existence,
    # configured mode). Generic decision code MUST NOT branch on this; it is
    # only for richer rendering/UX.
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderSnapshot:
    provider: str
    available: bool = True
    reason: str = ""
    source: str = ""
    selected_model: str | None = None
    scopes: list[CapacityScope] = field(default_factory=list)
    # Display-only per-model scopes (e.g. Claude's Sonnet-only weekly limit).
    # These are intentionally NOT consulted by ``decide()``: a model-specific
    # limit must never gate the whole provider. They exist purely so the usage
    # table can surface per-model rate limits alongside the aggregate window.
    model_scopes: list[CapacityScope] = field(default_factory=list)


@dataclass
class UsageDecision:
    provider: str
    usable: bool
    reason: str
    wait_until: int | None = None
    scopes: list[CapacityScope] = field(default_factory=list)
    exhausted: list[CapacityScope] = field(default_factory=list)


# --- Scope validation ---------------------------------------------------------


def valid_scopes_for_provider(provider: str) -> frozenset[str]:
    return PROVIDER_SCOPES.get(provider, frozenset({SCOPE_AUTO}))


def is_known_scope(value: str) -> bool:
    return value in ALL_SCOPES


def validate_scope(provider: str, scope: str) -> str:
    """Validate a ``--scope`` value for a given provider.

    Returns the (possibly normalised) scope. Raises ``ValueError`` for an
    unknown scope or a scope that does not apply to ``provider``.
    """
    if not is_known_scope(scope):
        raise ValueError(f"invalid --scope: {scope}")
    allowed = valid_scopes_for_provider(provider)
    if scope not in allowed:
        raise ValueError(
            f"--scope {scope!r} is not valid for {provider} "
            f"(use one of: {', '.join(sorted(allowed))})"
        )
    return scope


def effective_scopes(snapshot: ProviderSnapshot, scope: str) -> list[CapacityScope]:
    """Resolve the scope argument against a snapshot to concrete scopes.

    ``auto`` is provider-specific; for now it returns every known scope on
    the snapshot, in the order the provider declared them. Callers that want
    a different auto policy should layer it on top.
    """
    if scope == SCOPE_AUTO:
        return list(snapshot.scopes)
    return [s for s in snapshot.scopes if s.name == scope]


# --- Decision logic ------------------------------------------------------------


def _now(env: dict[str, str] | None = None) -> int:
    # Local import to avoid a circular import with common.
    from . import common

    return common.now_epoch(env)


def decide(
    snapshot: ProviderSnapshot,
    scope: str,
    min_remaining_percent: float,
    min_remaining_amount: float,
    poll_interval: int,
    *,
    cli_present: bool = True,
    env: dict[str, str] | None = None,
) -> UsageDecision:
    """Decide whether a provider is usable under a given scope.

    ``cli_present`` lets the caller (e.g. llm-scheduler) veto a snapshot that
    claims a balance/ungated scope is available when the underlying CLI is
    not actually installed. The default ``True`` preserves the snapshot's
    own ``available`` flag.
    """
    now = _now(env)
    poll = max(1, int(poll_interval))

    if not snapshot.available:
        return UsageDecision(
            provider=snapshot.provider,
            usable=False,
            reason=snapshot.reason or "unavailable",
            wait_until=now + poll,
            scopes=list(snapshot.scopes),
        )

    if not cli_present:
        return UsageDecision(
            provider=snapshot.provider,
            usable=False,
            reason="missing-cli",
            wait_until=now + poll,
            scopes=list(snapshot.scopes),
        )

    if scope == SCOPE_AUTO:
        scopes = list(snapshot.scopes)
    else:
        scopes = [s for s in snapshot.scopes if s.name == scope]

    if not scopes:
        return UsageDecision(
            provider=snapshot.provider,
            usable=False,
            reason="unsupported-scope",
            wait_until=now + poll,
            scopes=list(snapshot.scopes),
        )

    # Order matters for "auto": known-data scopes are evaluated first, so a
    # provider that reports both a budget and a balance (both required under
    # auto) only short-circuits on the first known-data failure.
    known: list[CapacityScope] = []
    inconclusive: list[CapacityScope] = []
    for s in scopes:
        if s.kind == CapacityKind.UNKNOWN or s.reason == "inconclusive-usage":
            inconclusive.append(s)
        else:
            known.append(s)
    if not known and not inconclusive:
        return UsageDecision(
            provider=snapshot.provider,
            usable=False,
            reason="unsupported-scope",
            wait_until=now + poll,
            scopes=scopes,
        )
    if not known:
        return UsageDecision(
            provider=snapshot.provider,
            usable=False,
            reason="inconclusive-usage",
            wait_until=now + poll,
            scopes=scopes,
        )

    blocked: list[CapacityScope] = []
    future_wait: list[int] = []
    for s in known:
        is_blocked, reason, wait = _scope_blocked(s, min_remaining_percent, min_remaining_amount, now)
        if not is_blocked:
            continue
        s.ready = False
        s.reason = reason
        blocked.append(s)
        if wait is not None:
            future_wait.append(wait)

    if not blocked:
        return UsageDecision(
            provider=snapshot.provider,
            usable=True,
            reason="usable",
            wait_until=None,
            scopes=scopes,
        )

    if future_wait:
        wait_until = max(future_wait)
    else:
        wait_until = now + poll
    exhausted_reason = _combined_block_reason(blocked)
    return UsageDecision(
        provider=snapshot.provider,
        usable=False,
        reason=exhausted_reason,
        wait_until=wait_until,
        scopes=scopes,
        exhausted=blocked,
    )


def _scope_blocked(
    scope: CapacityScope,
    min_percent: float,
    min_amount: float,
    now: int,
) -> tuple[bool, str, int | None]:
    """Return (is_blocked, reason, future_wait_epoch)."""
    if scope.kind == CapacityKind.UNGATED:
        return False, "", None
    if scope.kind == CapacityKind.UNKNOWN:
        return True, "inconclusive-usage", None
    if scope.kind == CapacityKind.OPAQUE:
        # Opaque capacity is never percent-gated. It is either ready (CLI
        # present, no local block) or the caller is expected to consult an
        # external block ledger; this function only returns the local
        # snapshot view, so it never marks an opaque scope blocked on its
        # own. The route layer records / clears blocks separately.
        return False, "", None
    if scope.kind == CapacityKind.RESET_WINDOW:
        rem = scope.remaining_percent
        if rem is None:
            return True, "inconclusive-usage", None
        # If the reset is in the past the window has already rolled over:
        # treat it as usable (this matches the legacy freshen_window logic).
        if scope.reset_epoch is not None and scope.reset_epoch <= now:
            return False, "", None
        if rem <= min_percent:
            return True, "rate-limited", scope.reset_epoch
        return False, "", None
    if scope.kind == CapacityKind.BUDGET:
        rem = scope.remaining_percent
        if rem is None:
            return True, "inconclusive-usage", None
        if scope.reset_epoch is not None and scope.reset_epoch <= now:
            return False, "", None
        if rem <= min_percent:
            wait = scope.reset_epoch if scope.reset_epoch is not None else None
            return True, "budget-exhausted", wait
        return False, "", None
    if scope.kind == CapacityKind.BALANCE:
        amount = scope.remaining_amount
        if amount is None:
            return True, "inconclusive-usage", None
        if amount < min_amount:
            return True, "insufficient-balance", None
        return False, "", None
    return True, "unsupported-scope", None


def _combined_block_reason(blocked: Iterable[CapacityScope]) -> str:
    blocked = list(blocked)
    if not blocked:
        return "blocked"
    if len(blocked) == 1:
        # A single blocked scope's reason is the most specific signal: a
        # reset_window with no data is `inconclusive-usage`, not
        # `rate-limited`. Surface it directly.
        return blocked[0].reason or "blocked"
    kinds = {s.kind for s in blocked}
    if kinds == {CapacityKind.RESET_WINDOW}:
        return "rate-limited"
    if kinds == {CapacityKind.BUDGET}:
        return "budget-exhausted"
    if kinds == {CapacityKind.BALANCE}:
        return "insufficient-balance"
    if kinds == {CapacityKind.OPAQUE}:
        # Opaque-only blocking is always a local runtime block; the caller is
        # expected to have already recorded it. Fall through to the generic
        # "blocked" word rather than inventing a quota-specific reason.
        return "blocked"
    # Mixed blocking kinds: report the most specific one first.
    order = [
        CapacityKind.BALANCE,
        CapacityKind.BUDGET,
        CapacityKind.RESET_WINDOW,
    ]
    for kind in order:
        if kind in kinds:
            return {
                CapacityKind.BALANCE: "insufficient-balance",
                CapacityKind.BUDGET: "budget-exhausted",
                CapacityKind.RESET_WINDOW: "rate-limited",
            }[kind]
    return "blocked"


# --- Pace calculation (for ralph even-burn) -----------------------------------


def scope_pace(scope: CapacityScope, now: int) -> float | None:
    """A scope's "remaining capacity per day" for rotation ranking.

    Higher = more headroom. ``None`` means the scope cannot be ranked (balance
    / ungated / unknown). Budget and reset_window scopes divide their
    remaining percent by the days until reset. A scope whose reset is
    already past is treated as a full window.
    """
    if scope.kind in (CapacityKind.BALANCE, CapacityKind.UNGATED, CapacityKind.UNKNOWN, CapacityKind.OPAQUE):
        return None
    rem = scope.remaining_percent
    if rem is None:
        return None
    if scope.reset_epoch is None or scope.reset_epoch <= now:
        # Stale or unknown reset: assume a generous window so we still rank.
        days = 7.0
    else:
        days = max((scope.reset_epoch - now) / 86400.0, 1.0)
    return float(rem) / days


def is_undetermined_reason(reason: str) -> bool:
    """A decision is undetermined unless it points at a known wait.

    Kilo balance/budget/ungated are usable or have short polling waits that
    should not block forever. Only the legacy "rate-limited" / "budget-
    exhausted" / "insufficient-balance" reason families might be anchored to
    a real future epoch worth waiting for.
    """
    return reason not in {"rate-limited", "budget-exhausted", "insufficient-balance"}


__all__ = [
    "ALL_KINDS",
    "ALL_PROVIDERS",
    "ALL_SCOPES",
    "CapacityKind",
    "CapacityScope",
    "ProviderSnapshot",
    "PROVIDER_CLAUDE",
    "PROVIDER_CODEX",
    "PROVIDER_COPILOT",
    "PROVIDER_KILO",
    "PROVIDER_MINIMAX",
    "PROVIDER_OPENCODE",
    "PROVIDER_SCOPES",
    "SCOPE_5H",
    "SCOPE_AUTO",
    "SCOPE_BALANCE",
    "SCOPE_BUDGET",
    "SCOPE_BYOK",
    "SCOPE_MONTHLY",
    "SCOPE_UNGATED",
    "SCOPE_WEEKLY",
    "UsageDecision",
    "decide",
    "effective_scopes",
    "is_known_scope",
    "is_undetermined_reason",
    "scope_pace",
    "validate_scope",
    "valid_scopes_for_provider",
]
