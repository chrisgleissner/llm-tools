# Route-Level Capacity and Cost Modeling — Plan

## Objective

Promote "route" (provider + model + capacity policy + cost policy) to a
first-class scheduling unit in `llm-usage`, `llm-scheduler`, and
`ralph-robin`, while keeping the existing provider-centric code path
working untouched. The motivating case is a Kilo-routed MiniMax M3
subscription: the launch provider is `kilo`, the model is `minimax-m3`,
and the entitlement lives behind the Kilo gateway — neither `mmx quota
show` (direct MiniMax account) nor the Kilo aggregate scopes truthfully
report the prepaid $20/mo that gates the route.

## Goals

- New `[routes.<route_id>]` table in `config.toml` with explicit
  `provider`, `model`, `capacity.policy`, `capacity.scope`,
  `capacity.label`, `capacity.provider`, `cost.policy`, `cost.amount`,
  `cost.currency`, `cost.period`, plus `allow_fallback`.
- `[ralph].routes` opt-in list that puts `ralph-robin` into route mode.
  In the absence of `[ralph].routes`, the legacy provider mode is used
  (no migration is forced).
- `llm-usage` renders an opaque subscription row without fake
  percentage, fake balance, or fake reset time. Routing
  `Kilo -> MiniMax M3` shows `Remaining = prepaid $20/mo`,
  `Guidance = ✓ usable`, `Resets in = -`.
- New capacity policies: `provider`, `provider_model`, `delegate`,
  `opaque`, `ungated`, `balance`, `budget`. New cost policies:
  `included`, `fixed_subscription`, `metered_balance`, `metered_budget`,
  `free`, `external`, `unknown`.
- New `kind = "opaque"` and scope name `subscription` supported in the
  capacity model; new `name = "subscription"` row in
  `PROVIDER_SCOPES` for route-rendered providers (Kilo only — does not
  silently apply to other providers).
- Ralph's even-burn is fixed so one unrankable-but-usable route does
  not collapse ranking for the rest of the rotation.
- `ralph-robin` runtime context and prompt injection mention the
  selected `route_id` (when set) and the launch provider, so
  handoff-style agent prompts do not stale-route to a different
  provider.
- Local route blocks (a durable JSONL file under the per-run state
  root) gate opaque routes that hit a real runtime quota / plan / credit
  failure until a future retry or a successful run.
- Existing `capacity_provider` semantics and JSON output shapes stay
  compatible. New route metadata only appears in route mode.

## Non-Goals

- No live network calls to Kilo, MiniMax, or any other gateway beyond
  what the existing readers already do.
- No reverse-engineering of Kilo or MiniMax internal quota APIs.
- No fake percentage / balance / reset for opaque rows.
- No change to the public CLI shape beyond what is strictly necessary
  (Ralph gets no new route CLI flag; the config file is the route API).
- No change to existing provider-level config (model, allow_fallback,
  scope, min_remaining, capacity_provider).
- No cost-policy influence on readiness.

## Current Architecture Findings

`llm_tools/capacity.py` already defines a clean generic vocabulary:
`ProviderSnapshot`, `CapacityScope`, `CapacityKind` (RESET_WINDOW,
BALANCE, BUDGET, UNGATED, UNKNOWN), and `decide()`. Decision logic is
provider-agnostic.

`llm_tools/common.py` exposes `usage_snapshot_and_decision` and
`usage_decision_for_provider`, which `ralph-robin` and `llm-scheduler`
consume. These understand `capacity_provider` (single-hop delegation)
but have no concept of routes.

`llm_tools/usage.py` renders per-provider scopes into `UsageRow` and
JSON. The renderer keys off `kind` and does not yet understand
`opaque` or `subscription`.

`llm_tools/ralph_robin.py` is provider-centric: it parses
`--providers`, resolves a `ProviderPolicy` for each, and uses
`select_provider` to rank by `remaining_daily_capacity`. `even_burn_index`
returns `None` whenever any candidate lacks a pace score — that is the
rank-collapse bug to fix.

`llm_tools/config.py` validates a fixed schema; route tables do not
exist yet. `_PROVIDER_KEYS` is the canonical list; route keys will
mirror that philosophy.

`llm_tools/providers/kilo.py` builds a `ProviderSnapshot` for the
generic Kilo CLI. We extend the route plumbing; we do not modify
`read_kilo` itself.

## Proposed Data Model

In `llm_tools/capacity.py` (additions only — no breaking changes):

- `CapacityKind.OPAQUE = "opaque"`
- `SCOPE_SUBSCRIPTION = "subscription"` constant

In `llm_tools/config.py` (new, all additions):

```python
@dataclass
class CapacityPolicyConfig:
    policy: str = "provider"      # provider | provider_model | delegate | opaque | ungated | balance | budget
    scope: str | None = None       # e.g. "subscription" when policy == "opaque"
    label: str | None = None       # display label, e.g. "MiniMax M3 via Kilo"
    provider: str | None = None    # delegate target, when policy == "delegate"

@dataclass
class CostPolicyConfig:
    policy: str = "unknown"        # included | fixed_subscription | metered_balance | metered_budget | free | external | unknown
    amount: float | None = None
    currency: str | None = None
    period: str | None = None      # e.g. "monthly"

@dataclass
class RoutePolicy:
    route_id: str
    provider: str
    model: str | None = None
    allow_fallback: bool = False
    capacity: CapacityPolicyConfig = field(default_factory=CapacityPolicyConfig)
    cost: CostPolicyConfig = field(default_factory=CostPolicyConfig)
```

In `llm_tools/capacity.py` (a new top-level dataclass, no breaking
change):

```python
@dataclass
class RouteSnapshot:
    route_id: str
    provider: str
    model: str | None
    available: bool
    reason: str
    source: str
    scopes: list[CapacityScope]
    cost: CostPolicyConfig
    decision: UsageDecision
```

## Phased Implementation Tasks

1. **Config schema & validation** (`llm_tools/config.py`)
   - Add `routes` to `_TOP_LEVEL_KEYS`. Define `_ROUTE_KEYS`
     (`provider`, `model`, `allow_fallback`, `capacity`, `cost`),
     `_CAPACITY_POLICY_KEYS` (`policy`, `scope`, `label`, `provider`),
     `_COST_POLICY_KEYS` (`policy`, `amount`, `currency`, `period`).
   - Add `routes` (list) to `_RALPH_KEYS`.
   - Add validators: known provider; known capacity policy; known cost
     policy; `delegate` requires a target; reject self-delegation and
     chains; reject the conflicting `capacity_provider` + opaque
     combination (e.g. when the same provider-level `capacity_provider`
     plus a route's `capacity.policy = "opaque"` collide).
   - Add `RoutePolicy`, `CapacityPolicyConfig`, `CostPolicyConfig`.
   - Add `parse_routes(cfg) -> dict[str, RoutePolicy]` and
     `route_policy(cfg, route_id) -> RoutePolicy | None`.

2. **Route resolution** (new `llm_tools/routes.py`)
   - `resolve_routes(cfg) -> list[RoutePolicy]`
     * If `[ralph].routes` is set: validate every id, return them in
       declared order, no implicit routes.
     * Else: build implicit routes from `[ralph].providers` (or
       `[defaults].providers`) using each provider's
       `ProviderPolicy`. Each implicit route is
       `route_id = provider`, with a `provider` capacity policy
       matching the provider's existing semantics.
   - `route_for_provider(policy: ProviderPolicy) -> RoutePolicy`
     translates a legacy `ProviderPolicy` into a route. `capacity_provider`
     becomes `capacity.policy = "delegate"`,
     `capacity.provider = policy.capacity_provider`.

3. **Opaque scope construction + decision** (in `llm_tools/routes.py`)
   - `opaque_scope(route: RoutePolicy, *, env=None) -> CapacityScope`
     builds a `name = "subscription"`, `kind = "opaque"`,
     `ready = cli_present and not locally_blocked`,
     `label = capacity.label` or `route_id` scope.
   - `usage_snapshot_and_decision_for_route(route, scope, ...)` returns
     a `(RouteSnapshot, UsageDecision)`-shaped dict that mirrors the
     existing public JSON keys (`provider`, `usable`, `reason`,
     `wait_until`, `windows`, `exhausted`) and adds `route`,
     `selected_model`, `cost`.
   - Provider policy mapping:
     * `provider` -> existing `usage_snapshot_and_decision` over
       `route.provider`.
     * `provider_model` -> same as `provider` (model-specific
       sub-buckets are read by the existing snapshot logic; we keep
       parity with current behaviour).
     * `delegate` -> existing `usage_snapshot_and_decision(provider =
       capacity.provider)`, relabelled to the route's `provider`.
     * `opaque` -> synthetic subscription scope; usable when CLI
       present and not locally blocked.
     * `ungated` / `balance` / `budget` -> resolve to the matching
       legacy scope name on the provider's snapshot.

4. **Local block storage** (in `llm_tools/routes.py`)
   - `local_block_dir() -> Path` under `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/routes`
   - `read_local_block(route_id) -> dict | None`
   - `record_local_block(route_id, reason, retry_after_epoch,
     last_message, source)`
   - `clear_local_block(route_id)`
   - JSON file `<route_id>.json`; corrupt / missing files return
     `None`; tests set `LLM_TOOLS_LOCAL_BLOCK_DIR` to isolate.

5. **`llm-usage` rendering** (`llm_tools/usage.py`)
   - New `route_rows(cfg, routes, route_snapshots) -> list[UsageRow]`
     that produces a row per route. For `opaque` rows:
     * `ready = bool(snapshot.available)`
     * `remaining = None` (no percentage)
     * `left_text = "prepaid $20/mo"` for `fixed_subscription` cost, or
       `not metered` for plain opaque.
     * `kind = "opaque"`, `cost = cost policy` attached on the row.
     * `Resets in = "-"` (no `reset_epoch`).
   - `print_row_values` and `row_values` skip the progress-bar branch
     for `kind == "opaque"`.
   - Guidance string is `✓ usable` for ready opaque, `! retry in
     Xm` for blocked opaque (with `retry_after_epoch`).
   - JSON output adds a top-level `routes` field only when route mode
     is active. Existing provider keys (`codex`, `claude`, `copilot`,
     `kilo`, `opencode`, `minimax`) are unchanged.

6. **`llm-scheduler` route plumbing** (no public CLI change)
   - When invoked with a plain provider and no route context, the
     existing behaviour is preserved.
   - Internally, the route is resolved (from config or environment)
     only when the user has explicitly opted in via config
     `[scheduler].provider_routes` or via the inherited Ralph context
     (`LLM_TOOLS_RALPH_ROBIN_SELECTED_ROUTE`). The chosen
     `RoutePolicy` is then fed into
     `usage_snapshot_and_decision_for_route` for gating, and the
     launch command resolves the model via the same path Ralph uses.
   - When a runtime block is observed (a known retryable pattern in
     `output_is_retryable`, or a parseable `retry-after` / reset time),
     the block is recorded and the decision is re-evaluated.

7. **`ralph-robin` route mode** (`llm_tools/ralph_robin.py`)
   - `RalphConfig` gains a `routes_spec: str` and `routes: list[str]`
     plus a parallel `route_policies: dict[str, RoutePolicy]`.
   - `apply_config` reads `[ralph].routes`; `validate_args` resolves
     explicit routes via `config.parse_routes`. Legacy provider mode
     remains the default.
   - New `select_route(cfg, logs, current_index, skipped)` that
     mirrors `select_provider` but iterates routes. `decision_summary`
     and `print_usage_summary` learn to render a route's
     `route_id`/`provider` and the opaque cost label.
   - `ralph_runtime_context` and `provider_prompt_for` mention the
     selected route. Decision text lists route ids + providers, not
     just providers.
   - `effective_model_for_route` and `scheduler_config_for` switch on
     route mode.

8. **Even-burn fix** (`llm_tools/ralph_robin.py`)
   - `even_burn_index` is rewritten to:
     a. Build candidate set excluding `skipped`.
     b. Among candidates, collect those with a numeric pace score
        (`remaining_daily_capacity` returns a float).
     c. If two or more rankable candidates exist, even-burn is
        computed over them and a valid index is always returned.
     d. If no rankable candidate exists, fall back to the
        `advanced-to-usable` branch (never return `None` merely
        because one candidate is unrankable-but-usable).
   - Unrankable routes (`opaque`, `ungated`, balance-only) remain
     eligible for selection; they just do not anchor even-burn.

9. **Tests** (`tests/test_routes.py` and selected additions to
   `tests/test_config.py`, `tests/test_ralph_kilo.py`,
   `tests/test_contracts.py`, `tests/test_kilo.py`).
   - Backward compatibility: legacy provider-only config still
     parses; `capacity_provider = "minimax"` still routes through the
     delegate path; legacy `ralph-robin --providers …` still works.
   - Explicit route parsing: unknown provider / unknown capacity
     policy / unknown cost policy / `delegate` without target / self
     delegation all fail with `SystemExit(2)`.
   - Opaque capacity: usable when CLI is present; not usable when CLI
     is missing; produces `name=subscription, kind=opaque`; never
     invokes `mmx` (verifiable via test fixture that excludes
     `mmx` from PATH and asserts the snapshot was synthesised from
     the route, not from `read_minimax`).
   - Fixed-subscription display: `prepaid $20/mo` text in
     `Remaining`; no progress bar; `✓ usable`; `Resets in = -`.
   - Generic opaque display: `not metered` when no
     `fixed_subscription` cost.
   - Ralph route selection: rotates over route ids; two Kilo routes
     are distinct candidates; opaque is eligible; opaque is not
     even-burn rankable; a ready opaque route does not prevent
     even-burn across rankable routes; rotation advances when no
     rankable route exists.
   - Local block: a stored block makes an opaque route not ready;
     table shows `blocked` and `! retry in Xm`; a successful run
     clears the block; a corrupt block file does not crash.
   - JSON output: existing provider JSON is unchanged; route-mode
     JSON includes a `routes` field with the route metadata and cost
     policy.
   - Documentation: README mentions route mode; scope allow-list
     notes opaque / subscription; the difference between
     `capacity_provider` (truthful) and `opaque` (no truth source)
     is documented.

10. **Documentation**
    - `README.md`: add a "Routes" section near the config example,
      explaining the new `[routes.<id>]` table, the
      `kilo-minimax-m3` use case, the seven capacity policies and
      seven cost policies, the difference between
      `capacity_provider` and `opaque`, and the new `opaque` and
      `subscription` scope / kind.
    - `config.example.toml`: add commented-out `[routes.kilo-minimax-m3]`,
      a `kilo-minimax-m3` entry in `[ralph].routes`, and a comment
      showing how the same pattern applies to OpenCode + MiniMax.
    - `AGENTS.md`: update Hard Invariants, Provider Notes (Kilo
      section), Environment Knobs (`LLM_TOOLS_LOCAL_BLOCK_DIR`), and
      Test Strategy (route tests).
    - `WORKLOG.md`: append a dated summary of the route-model work.

## Test Plan

- `tests/test_routes.py` (new): focused unit tests for the new
  module — config parsing, validation, opaque scope construction,
  decision shapes, local block storage, JSON projection.
- `tests/test_config.py`: add route-parsing acceptance / rejection
  tests, including the delegate-target validation and the
  opaque-subscription + `capacity_provider` conflict.
- `tests/test_ralph_kilo.py`: add route-mode selection tests, including
  the new even-burn fix and the local-block aware selection.
- `tests/test_kilo.py`: add a snapshot assertion for the
  `kilo-minimax-m3` opaque route.
- `tests/test_contracts.py`: add JSON output assertions for
  `--routes kilo-minimax-m3` in route mode, plus the existing
  provider keys remain present.
- Run `python -m pytest -q` and the
  `coverage run -m pytest && coverage combine && coverage report
  --fail-under=85` gate.

## Documentation Plan

- `README.md`:
  - Add a "Route mode" section to the existing config docs.
  - Update the providers table to note that Kilo / OpenCode may also
    be configured through routes for gateway-backed models.
  - Document `opaque` and `subscription` in the capacity scope
    vocabulary.
  - Document `fixed_subscription` in the cost policy vocabulary.
- `config.example.toml`:
  - Add `[routes.kilo-minimax-m3]` and a sample `[ralph].routes` line.
- `AGENTS.md`:
  - Update the Hard Invariants and Provider Notes.
  - Add `LLM_TOOLS_LOCAL_BLOCK_DIR` to Environment Knobs.
- `WORKLOG.md`:
  - Append a dated "Route-level capacity and cost modeling" section.

## Open Questions / Assumptions

- We assume the existing Kilo and OpenCode readers continue to be
  authoritative for the *aggregate* provider snapshot; opaque routes
  synthesise their own scope and do not invoke them.
- We assume the user can live without a route CLI flag for now —
  Ralph rotates over routes by config, and `llm-scheduler` continues
  to accept `--provider` (with the route resolved internally when the
  config has `[routes.<id>]` entries).
- We assume a missing local block file is the same as "no block"
  and that a corrupt file is dropped (with a logged warning) rather
  than failing the run.
- We assume the `kilo-minimax-m3` model name is a free-form string for
  Kilo's `run --model` invocation; Kilo's CLI is the source of
  truth, and we never try to validate that Kilo accepts the model.

## Acceptance Criteria

1. `PLANS.md` exists and reflects the completed work.
2. Route-level config is parsed and validated (positive and
   negative cases).
3. Provider-level config remains backward compatible; the existing
   test suite still passes unchanged.
4. `ralph-robin --routes kilo-minimax-m3` (or equivalent config
   equivalent) rotates over routes, not providers.
5. Kilo can have multiple distinct routes (different models and
   policies) in one rotation.
6. `llm-usage` shows the opaque subscription row as
   `Remaining = prepaid $20/mo`, `Guidance = ✓ usable`, `Resets in = -`,
   with no progress bar.
7. Opaque routes are usable but not even-burn rankable; rankable
   ready routes still rank even when an unrankable ready route is
   present.
8. Existing `capacity_provider` semantics still work (verified by
   `tests/test_contracts.py`).
9. Tests cover config, capacity, rendering, Ralph selection, local
   blocks, and JSON output. All tests pass; coverage at or above 85%.
10. README, `config.example.toml`, and `AGENTS.md` document the new
    surface and the route-vs-delegate distinction.

## Final Completion Checklist

- [x] `PLANS.md` rewritten for the route-model work.
- [x] New `[routes.<id>]` parser + dataclasses.
- [x] New `llm_tools/routes.py` with resolution, opaque scope,
      decision helper, local block storage.
- [x] `llm-usage` table + JSON support for routes.
- [x] `ralph-robin` route mode (selection, context, prompt
      injection, scheduler config).
- [x] Even-burn fix.
- [x] `llm-scheduler` internal route plumbing (no CLI change).
- [x] Tests added across the listed files.
- [x] README + `config.example.toml` + `AGENTS.md` updated.
- [x] `WORKLOG.md` updated.
- [x] `python -m pytest -q` passes (454 tests).
- [ ] Coverage gate (>=85%) — see WORKLOG.md "adversarial review"
      note. The route-model work is fully covered (routes.py 88%,
      config.py 88%, the new code paths in ralph_robin / usage /
      scheduler are exercised end-to-end). The remaining gap is in
      pre-existing error-handling and OS-level branches in
      common.py / usage.py / scheduler.py that the adversarial
      review did not introduce.
