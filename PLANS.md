# Kilo Code CLI & Capacity Scope Refactor — Plan

## Current Objective

Add Kilo Code CLI as a first-class provider for `llm-usage`, `llm-scheduler`, and
`ralph-robin`, and refactor the narrow "window" abstraction into an extensible
"capacity scope" abstraction that natively models:

* reset-bound quota scopes (`5h`, `weekly`, `monthly`),
* non-reset balance scopes (Kilo funding),
* optional budget scopes (Kilo monthly budget pacing),
* ungated/BYOK/local scopes (Kilo `byok` / `local` / `ungated`),
* unknown/inconclusive states.

This is a clean replacement. Backwards compatibility is not a goal; the old
`--window` flag and the `Window` column header are removed.

## Assumptions

* Tests can be updated freely. No external consumers depend on the old CLI.
* Deterministic env vars (`LLM_USAGE_KILO_*`, `LLM_USAGE_KILO_MODE`,
  `LLM_SCHEDULER_USAGE_JSON`, fake provider commands) are sufficient to test all
  Kilo behavior.
* The Kilo CLI binary may not be present in CI; we never require it.
* We may add a new module `llm_tools/capacity.py` for the generic
  provider/capacity abstraction, and `llm_tools/providers/kilo.py` for the Kilo
  reader. The current file layout keeps everything in `llm_tools/common.py` for
  small modules; a new module is justified because the abstraction is
  load-bearing.
* AGENTS.md and the README will be updated to reflect the new model and Kilo
  environment variables.

## Architecture Decisions

1. **New module `llm_tools/capacity.py`** defines generic
   `ProviderId`, `CapacityKind`, `CapacityScope`, `ProviderSnapshot`, and
   `UsageDecision` dataclasses + helpers. No provider-specific code in this
   module.
2. **Provider adapters** live alongside the existing readers
   (`read_codex`, `read_claude`, `read_copilot`, `read_kilo`). Each adapter
   returns a `ProviderSnapshot`.
3. **Kilo reader** `read_kilo` tries `kilo stats` output (if the binary
   exists), then falls back to environment variables. A new
   `llm_tools/providers/kilo.py` keeps the parser isolated.
4. **`--scope` flag** replaces `--window` everywhere. Valid scopes:
   `auto`, `5h`, `weekly`, `monthly`, `balance`, `budget`, `byok`, `ungated`.
   `auto` is provider-specific (see Implementation).
5. **Kilo command construction**:
   * Attached/interactive: `kilo run <prompt>` (in `--cwd`).
   * Headless/autonomous: `kilo run --auto <prompt>`.
   * We rely on `--cwd` to set the working directory, consistent with codex/copilot.
6. **Decision rules** live in `capacity.py`:
   * `reset_window`: behaves like today's `usage_decision_for_tool` for
     reset-bound providers.
   * `balance`: usable if `remaining_amount >= min_amount`; otherwise
     `insufficient-balance` with `wait_until = now + poll`.
   * `budget`: usable if `remaining_percent >= min_remaining`; otherwise
     `budget-exhausted` with `wait_until = reset_epoch` when known, else
     `now + poll`.
   * `ungated`: usable when the CLI is present; no reset data needed.
   * `unknown`/inconclusive: bounded polling.
7. **Ralph selection** uses `pace` from `CapacityScope` (computed for budget
   and reset_window scopes). Balance/ungated scopes are usable but not
   pace-rankable; they are used as fallbacks.

## Ordered Task List

- [x] Inspect current code, tests, and docs.
- [x] Create `PLANS.md` (this file) and `WORKLOG.md` first entry.
- [x] Add `llm_tools/capacity.py` with generic dataclasses + helpers.
- [x] Add `llm_tools/providers/__init__.py` and `llm_tools/providers/kilo.py`
  with the Kilo reader.
- [x] Wire Kilo into `read_*` family: `read_kilo` returning a snapshot.
- [x] Add `usage_snapshot_for_tool` and `usage_decision_for_tool` support for
  `kilo` and new scope types.
- [x] Replace `--window` with `--scope` in `llm-scheduler` and `ralph-robin`.
- [x] Update `validate_tool_window` → `validate_tool_scope`.
- [x] Update `UsageRow.window` field to `scope` (rename) and the table column
  from `Window` to `Scope`.
- [x] Add Kilo command construction in `provider_default_argv` /
  `highlight_provider_text`.
- [x] Update Ralph selection to consider pace-rankable vs usable-but-not-rankable
  providers.
- [x] Update Ralph runtime context wording.
- [x] Update `usage_prefix_text` to use new fields.
- [x] Update JSON top-level to include `kilo` (kept stable: `generated_at`,
  `codex`, `claude`, `copilot`, `kilo`).
- [x] Update `AGENTS.md` and `README.md`.
- [x] Add tests for Kilo, capacity decisions, scope validation, scheduler
  command construction, Ralph selection.
- [x] Run full test suite and ensure ≥85% coverage.

## Progress State

* Current: planning complete; about to start implementation.
* Blockers: none.

## Test Plan

* `tests/test_capacity.py` — generic abstraction unit tests.
* `tests/test_kilo.py` — Kilo reader, command construction, env-var-only mode.
* Update `tests/test_contracts.py` and `tests/test_additional_paths.py`:
  - Drop `--window` references; use `--scope`.
  - Add Kilo contract paths.
  - Add Kilo scheduling decisions.
  - Add Ralph selection with mixed reset-bound and Kilo providers.
* Keep using `LLM_SCHEDULER_USAGE_JSON` for the scheduler-side decisions
  where useful.
* Run `pytest -q` and `coverage run -m pytest && coverage combine && coverage
  report --fail-under=85`.

## Documentation Plan

* `README.md`:
  - Replace `Window` references with `Scope`.
  - Add Kilo row to the providers table.
  - Add a "Capacity scope model" section explaining the four kinds.
  - Add Kilo setup + env vars section.
  - Add scheduler / ralph examples for Kilo.
  - Note that Kilo is not forced into a fake session window.
* `AGENTS.md`:
  - Update Hard Invariants / Provider Notes.
  - Add Kilo environment variables.
  - Mention the new abstraction.
* `WORKLOG.md`:
  - Append a timestamped section for this refactor.

## Open Questions

* None blocking; all scope/value decisions are pinned by the task description.

## Final Completion Checklist

* [x] `kilo` accepted in `--tools`, `--tool`, and `validate_tool_scope`.
* [x] Kilo can be launched by `llm-scheduler` (attached + headless).
* [x] `ralph-robin --tools` includes `kilo`.
* [x] `llm-usage` table uses `Scope` column header.
* [x] Capacity scope abstraction in `llm_tools/capacity.py`.
* [x] Codex/Claude/Copilot still work via the new abstraction.
* [x] Kilo balance, budget, and BYOK/local/ungated behavior covered.
* [x] Tests cover Kilo command construction, scope validation, capacity
      decisions, Kilo balance decisions, Kilo budget pacing, Ralph selection.
* [x] README + AGENTS.md explain the new model + Kilo.
* [x] All relevant tests pass; coverage ≥85% (160 tests, 85% coverage).
* [x] `PLANS.md` and `WORKLOG.md` updated with evidence.
* [x] All four providers (kilo, codex, claude, copilot) factored into
      dedicated `llm_tools/providers/<name>.py` modules with a
      consistent `read(env) -> ProviderSnapshot` contract.
* [x] `llm_tools/providers/__init__.py` documents the 5-step recipe for
      adding a new provider.

## Evidence

* `python -m pytest -q` → 160 passed in ~55s.
* `coverage run -m pytest && coverage combine && coverage report
  --fail-under=85` → 85% total coverage.
* `llm-usage` with Kilo env vars renders balance/budget rows in the
  table; `--json` includes a `kilo` key with `scopes` for the
  generic ProviderSnapshot.
* `llm-scheduler --tool kilo --scope byok --dry-run` resolves the
  byok ungated scope.
* `llm-scheduler --tool kilo --scope balance --dry-run` resolves the
  balance scope with the configured minimum.
* `ralph-robin --tools kilo --max-iterations 1` selects Kilo via
  the generic selector and runs the provider cleanly.
