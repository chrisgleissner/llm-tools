# AGENTS.md

## Scope

This repo contains small Linux Python CLIs for Codex, Claude Code, GitHub Copilot, and Kilo Code CLI:

* `llm-usage` — show local usage/quota for each provider.
* `llm-scheduler` — submit a prompt to a provider CLI once usage data says it is usable (optionally waking/suspending around a scope reset).
* `ralph-robin` — keep using one configured provider until it is exhausted, then rotate to the next provider and delegate launch/suspend behavior to `llm-scheduler`. Holds an OS idle inhibitor for the whole run so a desktop idle timer cannot suspend the machine mid-work, and when every provider is rate-limited it sleeps the machine itself via a verified RTC wake (see Suspend/wake reliability below).
* `llm-sleep-soak` — repeatedly suspend and wake the machine using the exact production suspend path to prove sleep/resume is reliable on this hardware. Real-hardware test; cannot run in CI.
* `llm_tools/common.py` — shared helpers (provider readers, normalization, time/reset formatting, subprocess execution, usage decisions, PTY capture, wake diagnostics, and common CLI plumbing: argument validation, run-dir logging, prompt loading, argv/JSON conversion).
* `llm_tools/capacity.py` — generic `ProviderId`, `CapacityKind`, `CapacityScope`, `ProviderSnapshot`, and `UsageDecision` dataclasses plus the `decide`/`validate_scope`/`scope_pace` helpers. All provider-specific reader code lives outside this module.
* `llm_tools/providers/kilo.py` — Kilo Code CLI adapter (parser for `kilo stats` output, env-var fallback, command construction).
* `llm_tools/providers/minimax.py` — MiniMax adapter (parser for `mmx quota show --output json` output, env-var fallback, command construction).
* Python modules: `llm_tools/usage.py`, `llm_tools/scheduler.py`, `llm_tools/ralph_robin.py`, `llm_tools/sleep_soak.py`, `llm_tools/copilot_refresh.py`, and package marker `llm_tools/__init__.py`.
* Public direct-run command files: `llm-usage`, `llm-scheduler`, `ralph-robin`, `llm-sleep-soak`.
* Regression tests: `tests/` with pytest and fake provider commands.
* Test helpers: `tests/conftest.py`; main suites: `tests/test_contracts.py`, `tests/test_additional_paths.py`, `tests/test_capacity.py`, `tests/test_kilo.py`, `tests/test_minimax.py`, `tests/test_ralph_kilo.py`.
* Project/package config: `pyproject.toml`.
* Import/test bootstrap: `sitecustomize.py`.
* CI: `.github/workflows/test.yml`.
* User docs: `README.md`.
* Local planning/work logs: `PLANS.md`, `WORKLOG.md`.
* Runtime data root: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools`, one subdirectory per tool. Legacy `~/.cache/llm-usage`, `~/.cache/llm-scheduler`, and `~/.cache/ralph-robin` dirs are auto-migrated by `migrate_legacy_cache_dirs` in `llm_tools/common.py`.
* Usage cache and samples log: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-usage` (`claude-status.json`, `claude-usage-api.json`, `llm-usage.log`)
* Copilot background refresh helper: `llm_tools/copilot_refresh.py`, launched by `read_copilot` for detached cache refreshes.
* Scheduler run logs: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-scheduler/logs`
* Ralph Robin run logs: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/logs`
* Ralph Robin state: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/state.json`
* Suspend cycle ledger (durable, fsync'd; shared by ralph-robin and the soak): `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/suspend-ledger.jsonl`
* Sleep-soak run logs: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-sleep-soak/logs`

Keep these as dependency-light Python CLIs sharing helper modules: no daemon, server, database, telemetry, or broad provider SDK design unless explicitly requested. Shared logic belongs in `llm_tools/common.py` and `llm_tools/capacity.py`, not duplicated across CLIs or provider adapters.

## Fast checks

```bash
chmod +x llm-usage llm-scheduler ralph-robin llm-sleep-soak
./llm-usage
./llm-usage --json
./llm-usage --show-source --show-remaining-time
./llm-usage --hide-remaining-time --show-source
./llm-usage --show-copilot-credits --show-source
./llm-usage --hide-codex-spark
./llm-scheduler --provider codex --prompt x --dry-run --command-template true
./llm-scheduler --wake-test
./ralph-robin --prompt x --dry-run --command-template true
LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1 ./llm-sleep-soak --cycles 2 --period 5s --gap 0   # simulated; real runs actually suspend
python -m pytest -q
coverage run -m pytest && coverage combine && coverage report --fail-under=85
```

Statusline mode reads Claude statusline JSON from stdin:

```bash
printf '%s\n' '{"rate_limits":{"five_hour":{"used_percentage":10}}}' | ./llm-usage --statusline
```

## Implementation map

* CLI/setup: module `main` functions, argument parsing, `render_once`, watch dispatch
* Provider readers: `read_codex`, `read_claude_api`, `read_claude`, `read_copilot`
* Normalization: `normalize_codex_obj`, `normalize_claude_obj`, Copilot parse helpers
* JSON: `json_for_provider`, `json_for_copilot`, JSON branch in `render_once`
* Table rendering: `print_cell`, `print_value_row`, `print_row`, `print_unavailable_rows`, `print_codex_rows`, `print_copilot_rows`
* Remaining-time logic: `log_usage_sample`, `estimate_remaining_time_from_log`
* Time/reset formatting: `now_epoch`, `parse_epoch`, `fmt_reset`, `fmt_duration`, `time_until`
* Scheduler gates and launch: `usage_decision_for_tool`, `wait_until_usable`, `schedule_resume_and_suspend`, `command_argv`, `submit_once`, `run_fresh_headless`, `run_fresh_exact_stdout`, `run_tmux`
* Ralph Robin rotation: `select_tool`, `scheduler_config_for`, `run_scheduler_inline`, state helpers, status/highlight helpers
* Suspend/wake reliability (`common.py`): `power_backend`, `IdleSuspendInhibitor`, `arm_rtc_wake`, `read_rtc_wakealarm`, `Watchdog`, `suspend_with_wake`, `wall_clock_wait_until`, ledger helpers (`ledger_record_start`/`ledger_record_done`/`incomplete_suspend_cycles`); ralph wiring: `guard_against_auto_suspend`, `report_prior_suspend_failures`, `SuspendState`, `suspend_block_reason`, `suspend_machine_until`
* Sleep soak: `llm_tools/sleep_soak.py` (`run_cycle`, `summarize`, `scrape_resume_errors`, `main`)

Prefer changing the smallest relevant function surface. Preserve existing function boundaries unless a helper clearly reduces duplication or risk.

## Hard invariants

* Keep Python code typed, explicit, and standard-library-first.
* Missing data must degrade gracefully as `-`, `unknown`, or `unavailable`, never as empty cells or script failure.
* One provider failing must not block other provider rows.
* Table and JSON must agree on provider availability and values.
* Keep at least three visible spaces between table columns.
* Keep color disabled for non-TTY output, `TERM=dumb`, `NO_COLOR`, or `LLM_USAGE_NO_COLOR`.
* Ralph/scheduler highlighting should default to a readable green/blue/teal palette that works on typical dark and light terminals. Keep colors centralized in `common.ANSI_COLOR_ROLES` and configurable through `LLM_TOOLS_COLOR_<ROLE>` rather than hard-coding ANSI codes at call sites.
* Ralph/scheduler live output may use compact UTF-8 symbols to distinguish status, command, tool-call, stderr, diff hunk, and error blocks. Keep symbols centralized in `common.UTF_SYMBOL_ROLES`, configurable through `LLM_TOOLS_SYMBOL_<ROLE>`, and suppressible with `LLM_TOOLS_NO_SYMBOLS=1`.
* Keep JSON top-level keys stable: `generated_at`, `codex`, `claude`, `copilot`, `kilo`, `minimax`. A top-level `routes` key is added when at least one `[routes.<id>]` is configured; the existing provider keys remain unchanged.
* Keep Copilot unavailable shape explicit: `available:false`, with `reason` when known.
* Keep option semantics stable: `--show-source`, `--hide-source`, `--show-remaining-time`, `--hide-remaining-time`, `--show-codex-spark`, `--hide-codex-spark`, `--show-copilot-credits`.
* The `--scope` flag replaces the legacy `--window` flag. `--window` is accepted as a deprecated alias and should not appear as the primary documented interface.
* Scope names (current): `auto`, `5h`, `weekly`, `monthly`, `balance`, `budget`, `byok`, `ungated`, `subscription`. Provider-specific allow-lists live in `capacity.PROVIDER_SCOPES`. The `subscription` scope is a route-only display name; it does not appear in the per-provider allow-lists.
* Capacity kinds (generic, in `llm_tools/capacity.py`): `reset_window`, `balance`, `budget`, `ungated`, `unknown`, `opaque`. Generic scheduler/rotation code must reason about kinds, not provider-specific window names. The `opaque` kind describes capacity that exists but cannot be measured before launch; the readiness gate is "CLI present + no local runtime block", never a percent threshold.
* Do not model Kilo as opaque globally. Only specific Kilo routes may be opaque.
* Route-level capacity policies (`llm_tools/routes.py`): `provider`, `provider_model`, `delegate`, `opaque`, `ungated`, `balance`, `budget`. Cost policy never affects readiness.
* Keep Codex Spark matching by key `codex-spark` or name containing `spark`.
* Remaining-time estimation must return `-` when confidence is insufficient.
* Do not log secrets, tokens, credential files, or raw sensitive provider payloads.

## Provider notes

### Codex

Read local JSONL under `~/.codex/sessions`. Keep selectors tolerant of `rate_limits`, `rateLimits`, `msg`, and `payload` shapes. Keep bounded scans through `LLM_USAGE_MAX_FILES` and `LLM_USAGE_TAIL_LINES`.

### Claude Code

Preserve fallback order: API/cache/statusline/local project data. `--statusline` must keep caching stdin JSON for later use. API failure must fall back cleanly.

### GitHub Copilot

Tests should use `LLM_USAGE_COPILOT_CAPTURE_TEXT` or bounded timeout paths, not live Copilot state. Keep `LLM_USAGE_DISABLE_COPILOT=1` reliable. If footer parsing fails, report unavailable with a reason rather than inventing values.

The PTY capture is slow (up to `LLM_USAGE_COPILOT_TIMEOUT` seconds), so `read_copilot` serves a cached snapshot (`copilot-usage.json`, TTL `LLM_USAGE_COPILOT_CACHE_TTL`, default 300s) and revalidates it with a detached background capture. The fixture/override knobs above and `LLM_USAGE_COPILOT_CACHE_TTL=0` force the original synchronous capture; keep that bypass intact so tests stay deterministic.

### Kilo Code CLI

Tests should use the env-var fallback path (`LLM_USAGE_KILO_*`) and `LLM_USAGE_COPILOT_CAPTURE_TEXT`-style fixture overrides rather than live `kilo stats` capture. The reader tries `kilo stats` first (JSON or human-readable) and falls back to:

* `LLM_USAGE_KILO_MODE` — `gateway` (default), `budget`, `byok`, `local`, `ungated`.
* `LLM_USAGE_KILO_BALANCE` — remaining credits/balance.
* `LLM_USAGE_KILO_CURRENCY` — currency or unit label.
* `LLM_USAGE_KILO_MIN_BALANCE` — minimum balance to consider usable (default 1).
* `LLM_USAGE_KILO_MONTHLY_BUDGET` / `LLM_USAGE_KILO_MONTHLY_SPENT` — budget pacing.
* `LLM_USAGE_KILO_MONTHLY_RESET_DAY` — day of month the budget resets (default 1).

Missing CLI in BYOK/local/ungated mode is `reason="missing-cli"`. Missing data in gateway mode is `reason="inconclusive-usage"`. Kilo is not forced into a fake session window; its only scope is `balance`, `budget`, or `ungated`.

### Route model

When the same provider can serve several underlying models with different capacity and cost semantics (e.g. Kilo selling `minimax-m3` as a prepaid gateway subscription), use a route (`[routes.<id>]` in the config) instead of a plain provider. Routes are resolved by `llm_tools/routes.resolve_routes`. An explicit `[ralph].routes` list puts `ralph-robin` into route mode; in its absence the legacy provider rotation is used.

* `capacity.policy = "opaque"` → `name="subscription"`, `kind="opaque"`, no percent / reset. The route is ready when the launch CLI is on `PATH` and there is no local block; the table reads `Remaining = prepaid $20/mo`, `Guidance = ✓ usable`, `Resets in = -`.
* `capacity.policy = "delegate"` is the route-level successor to the legacy `providers.<x>.capacity_provider` setting. The provider-level setting still works (it is mapped to an implicit route with `capacity.policy = "delegate"`).
* Cost policy is display metadata only: `included`, `fixed_subscription`, `metered_balance`, `metered_budget`, `free`, `external`, `unknown`. It never affects readiness.
* Local blocks for opaque routes live under `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/routes/blocks/<route_id>.json` (override with `LLM_TOOLS_LOCAL_BLOCK_DIR`). One corrupt or missing file is treated as "no block" so a one-off bad write cannot break the orchestrator.
* `llm-usage` renders a route row prefixed with `route:<route_id>` in the Provider column and the model name in the Model column when the Model column is enabled. No progress bar is rendered for opaque / fixed-subscription rows.

### MiniMax

The MiniMax provider is sourced from the `mmx` CLI (run `mmx quota show --output json`). The reader tries that first and falls back to a deterministic env-var schema for tests. It picks the `general` row from the `model_remains` array (other model rows are ignored) and exposes the same 5h/weekly reset-window shape as Claude Code and Codex, so the row renders and gates identically to the other two reset-window providers. The provider is hidden entirely from the table and `--json` output when the `mmx` binary is not on PATH and no env-var fallback is present.

Tests should use the env-var fallback path (`LLM_USAGE_MINIMAX_*`) and `LLM_USAGE_COPILOT_CAPTURE_TEXT`-style fixture overrides rather than live `mmx` capture. The reader tries `mmx quota show --output json` first and falls back to:

* `LLM_USAGE_MINIMAX_5H_PERCENT` — remaining percent for the 5h session window (0..100).
* `LLM_USAGE_MINIMAX_5H_RESET_EPOCH` — epoch seconds (or milliseconds) when the 5h window resets.
* `LLM_USAGE_MINIMAX_WEEKLY_PERCENT` — remaining percent for the weekly window.
* `LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH` — epoch seconds (or milliseconds) when the weekly window resets.
* `LLM_USAGE_MINIMAX_MODEL` — which `model_remains` row to read (default `general`).
* `LLM_USAGE_MINIMAX_TIMEOUT` — `mmx quota show` timeout in seconds (default 10).

Missing CLI with no env-var fallback is `reason="missing-cli"`. The provider is allowed only when at least one of the two scopes (5h, weekly) has data; the reader does not invent a fake window.

## Scheduler invariants

* `llm-scheduler` gates on the same `llm_tools.common` provider readers as `llm-usage`; tests inject usage via `LLM_SCHEDULER_USAGE_JSON` and the command via `--command-template`, never live providers.
* A `rate-limited` decision (a known window with a real reset epoch) must wait for that reset, not proceed early.
* An *undeterminable* decision (`unavailable`, `inconclusive-usage`, `unsupported-window`) must never block forever: bound the wait with `--max-unavailable-wait`, then launch optimistically. See `is_undetermined_reason`.
* `--window` must be valid for the provider (copilot: auto/monthly; codex/claude: auto/5h/weekly). Reject other combinations in `validate_args`.
* Treat a provider launch as needing retry on non-zero exit, or on a clean exit whose output clearly signals a provider rate-limit/overload. Keep `output_is_retryable` patterns specific so ordinary successful agent output is not re-submitted. The synthetic autonomy-abort status `75` is different: do not retry the same provider session inside `llm-scheduler`.
* Under `--wake`, arm at most one OS wake timer per distinct, far-enough target (`log_wake_plan` lead guard + `WAKE_ARMED_TARGET`); never one per poll iteration.
* Never log secrets; prompt copies live under the run dir with `600`/`700` perms.
* Fresh mode on an interactive terminal runs the provider CLI in its normal interactive form on a PTY wired directly to that terminal via `script(1)` (`resolve_attach_mode`, `ATTACHED=1`): output, stdin, resizes, and Ctrl-C must behave exactly as a direct CLI launch. Headless fresh mode (no TTY, `--headless`, `LLM_SCHEDULER_HEADLESS=1`, or `LLM_SCHEDULER_NO_STREAM=1`) keeps the non-interactive provider commands and streams the child output live to the scheduler's stdout (and through `ralph-robin` to the invoking terminal) unless `LLM_SCHEDULER_NO_STREAM=1`. Both paths write the ANSI-cleaned copy to `attempt-N.out`. Attached runs never retry on a clean exit or user cancel (130/143) and skip the rate-limit phrase grep, since interactive screen content can legitimately mention rate limits. Headless runs must abort with status `75` when a blocking prompt UI is detected, when question-like output stalls, or when there is no output progress past `LLM_SCHEDULER_IDLE_TIMEOUT`; `ralph-robin` must treat status `75` as a reason to re-evaluate rotation, not as a final failure after the first provider. Tests extract the run dir from the `logs written to` stdout line, never via `awk '{print $NF}'` over all lines.
* Ralph must prepend provider-aware runtime context before launching a selected provider. That context must identify the selected provider, list latest usage decisions, and override stale provider-specific handoff/scheduler instructions in the original prompt so Codex does not hand off merely because Claude is exhausted, and vice versa.
* Every provider is actively refreshed on read; `stale-usage` is a bug to display except for a known authentication or CLI-startup problem. Caches are bounded by a freshness window (TTL); never display a snapshot past its TTL — refresh it first (the Copilot reader waits for the in-flight capture rather than serving a stale snapshot and refreshing "for next time"). Active network refreshes retry a bounded number of times (`LLM_USAGE_LIVE_FETCH_RETRIES`) so a transient blip (e.g. a network stack still settling right after resume) does not degrade to `stale-usage`.
* Burn-rate / remaining-time estimation despikes isolated single-sample outliers before anchoring to the current window. A lone bad reading and its recovery must not be mistaken for a window reset (which would discard real history and surface a spurious `no rate data`). `no rate data` is correct only when there is genuinely no consumption to forecast (e.g. a window sitting at 100%) or the scope has no time-based pace (balance/ungated).

## Suspend/wake reliability

`ralph-robin` and `llm-sleep-soak` share one hardened suspend path, `common.suspend_with_wake`, behind a feature-detected backend seam (`common.power_backend`; `systemd` today). Keep this portable: detect capabilities with `have_cmd`, never branch on distro strings, and degrade to an in-process wall-clock wait (machine stays awake) when a capability is missing. A future macOS backend should implement the same primitives (`caffeinate`, `pmset schedule wake`) without touching callers.

* Hold a logind `idle` inhibitor (`common.IdleSuspendInhibitor`, via `systemd-inhibit --what=idle`) for the whole run so a desktop idle timer (KDE PowerDevil, GNOME, logind `IdleAction`) cannot suspend the machine mid-work with no wake armed. An `idle` inhibitor must NOT block an explicit `systemctl suspend`, so the orchestrator keeps control of its own deliberate suspends. The lock is pipe-backed so it releases on process exit/crash; `LLM_TOOLS_NO_INHIBIT=1` disables it (tests set this).
* Never suspend without a wake armed. Arm via `rtcwake -m no` when privileged (verifiable by reading back `/sys/class/rtc/rtc0/wakealarm`), else a `systemd-run --user` `WakeSystem=true` timer. If arming fails, do not suspend — fall back to an awake wait.
* Verify wakes by behaviour: after resume, compare the wall clock to the target (`LLM_TOOLS_SUSPEND_DRIFT_TOLERANCE`). A wake that lands far from target is unreliable; ralph latches to awake-only for the rest of the run rather than risk repeating it.
* Cap churn: a minimum awake interval between suspends (`LLM_RALPH_MIN_AWAKE_SECONDS`) and an optional per-run cap (`LLM_RALPH_MAX_SUSPENDS`) keep flaky hardware from being cycled repeatedly while unattended.
* Durable ledger: write a fsync'd `suspend_start` before `systemctl suspend` and a `suspend_done` after resume. A start with no done is the fingerprint of a wedged resume from a prior boot; both tools report it on startup.
* `--watchdog` is opt-in recovery for a wedged resume (arm a hardware watchdog across suspend so a hang reboots the box). It needs a usable `/dev/watchdog` and a watchdog that keeps counting across S3; absent that it is a logged no-op. Document the limitation, never hard-fail.
* `LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1` simulates a perfect cycle (no real sleep) so the orchestration is testable; tests must never actually suspend the host. Codex refreshes via the `codex app-server` JSON-RPC `account/rateLimits/read` method (live, turn-free), then a cached payload, then the local `~/.codex/sessions` JSONL. A missing `codex` binary reports `missing-cli`; absent `~/.codex/auth.json` credentials report `not-authenticated`. Mirror this contract for any new provider: prefer a live query, fall back to cache/local, and surface an auth/startup reason rather than old numbers.

## Environment knobs

Important knobs that tests or users may rely on:

* `LLM_USAGE_NO_COLOR`
* `LLM_USAGE_SHOW_SOURCE`
* `LLM_USAGE_SHOW_REMAINING_TIME`
* `LLM_USAGE_SHOW_CODEX_SPARK`
* `LLM_USAGE_NOW_EPOCH`
* `LLM_USAGE_MAX_FILES`
* `LLM_USAGE_TAIL_LINES`
* `LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE` (seconds before active/unknown Codex/Claude local snapshots are reported as stale; capped at 60; non-positive/invalid values fall back to 60)
* `LLM_USAGE_LIVE_FETCH_RETRIES` (extra attempts for active-refresh network reads before falling back; default 2; tests pin 0)
* `LLM_USAGE_LIVE_FETCH_RETRY_DELAY` (seconds between live-fetch retries; default 0.5)
* `LLM_TOOLS_NO_INHIBIT` (set to `1` to skip the logind idle inhibitor `ralph-robin`/soak hold during a run; tests set this)
* `LLM_TOOLS_LOCAL_BLOCK_DIR` (override the per-route local block directory; default `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/routes/blocks`)
* `LLM_TOOLS_LOCAL_BLOCK_BACKOFF` (default backoff in seconds for an opaque route runtime block when no `retry-after` hint is present; default 300)
* `LLM_TOOLS_RTC_WAKEALARM` (override the RTC wakealarm sysfs path; default `/sys/class/rtc/rtc0/wakealarm`; mainly tests)
* `LLM_TOOLS_WATCHDOG_DEVICE` (watchdog device path for `--watchdog`; default `/dev/watchdog`)
* `LLM_TOOLS_NO_WATCHDOG` (set to `1` to make `--watchdog` a no-op even when a device exists)
* `LLM_TOOLS_SUSPEND_DRIFT_TOLERANCE` (seconds a wake may land from target and still count as reliable; default 90, floored at 5)
* `LLM_TOOLS_WAIT_POLL_SECONDS` (wall-clock poll chunk for `common.wall_clock_wait_until`; default 30)
* `LLM_RALPH_MIN_AWAKE_SECONDS` (minimum awake time between machine suspends; default 60)
* `LLM_RALPH_MAX_SUSPENDS` (max machine suspends per run; default 0 = unlimited)
* `LLM_SCHEDULER_SUSPEND_MIN_LEAD` (minimum lead before arming a wake / suspending; default 120)
* `LLM_USAGE_PROVIDER_PARALLELISM` (provider reader fan-out concurrency for `llm-usage`; default is CPU cores)
* `LLM_USAGE_NO_PROGRESS` (set to `1` to suppress the ephemeral stderr refresh spinner; it is also auto-suppressed when stderr is not a TTY)
* `LLM_USAGE_CODEX_TIMEOUT` (seconds to wait for the `codex app-server` rate-limit handshake; default 15)
* `LLM_USAGE_DISABLE_CODEX_APP_SERVER` (set to `1` to skip the live Codex app-server refresh and use only the cache/local fallback; used by tests for hermetic runs)
* `LLM_USAGE_CODEX_APP_SERVER_CMD` (override the `codex app-server` command, e.g. a fake server in tests)
* `LLM_USAGE_CODEX_RATE_LIMITS_JSON` (inject a raw `account/rateLimits/read` payload, bypassing the subprocess; used by tests)
* `LLM_USAGE_LOG_TAIL_LINES`
* `LLM_USAGE_REMAINING_TIME_STALE_MULTIPLIER`
* `LLM_USAGE_REMAINING_TIME_MAX_STALE_SECONDS`
* `LLM_USAGE_DISABLE_COPILOT`
* `LLM_USAGE_COPILOT_TIMEOUT`
* `LLM_USAGE_COPILOT_CAPTURE_TEXT`
* `LLM_USAGE_COPILOT_CAPTURE_CMD`
* `LLM_USAGE_COPILOT_CACHE_TTL` (seconds a cached Copilot snapshot stays fresh; 0 forces synchronous capture)
* `LLM_USAGE_COPILOT_REFRESH_WAIT` (seconds to wait for a background Copilot refresh before serving stale data)
* `LLM_USAGE_COPILOT_CWD`
* `LLM_USAGE_COPILOT_CAPTURE_CWD`
* `LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS`
* `LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS`
* `LLM_SCHEDULER_NO_STREAM` (disable live pass-through of the child CLI output to stdout in fresh mode; also forces headless commands)
* `LLM_SCHEDULER_HEADLESS` (force the non-interactive provider command and captured PTY even on a terminal)
* `LLM_SCHEDULER_USAGE_JSON` (test: inject a usage snapshot)
* `LLM_SCHEDULER_NO_ACTUAL_SUSPEND` (test: skip the real `systemctl suspend`)
* `LLM_SCHEDULER_PTY_TIMEOUT` (headless fresh-process launch timeout, seconds; attached terminal runs have no timeout)
* `LLM_SCHEDULER_IDLE_TIMEOUT` (headless idle watchdog; abort when no output progress is seen for this many seconds; 0 disables)
* `LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT` (headless question watchdog; abort when question-like output stops progressing for this many seconds; 0 disables)
* `LLM_SCHEDULER_TMUX_TIMEOUT` (tmux completion timeout, seconds)
* `LLM_SCHEDULER_WAKE_MIN_LEAD` (min seconds before a target to bother arming an OS wake timer)
* `LLM_TOOLS_COLOR_<ROLE>` (override one Ralph/scheduler ANSI SGR color role; roles: `BRAND`, `INFO`, `OK`, `WARN`, `ERROR`, `DIM`, `DIFF_ADD`, `DIFF_REMOVE`, `DIFF_HUNK`, `COMMAND`, `TOOL`, `STDERR`, `HEADING`)
* `LLM_TOOLS_SYMBOL_<ROLE>` (override one Ralph/scheduler UTF-8 symbol role; same roles as `LLM_TOOLS_COLOR_<ROLE>`)
* `LLM_TOOLS_NO_SYMBOLS` (disable Ralph/scheduler live-output symbols while keeping color enabled)
* `LLM_TOOLS_RALPH_ROBIN_ACTIVE` (internal/inherited guard: provider subprocesses launched by `ralph-robin` set this to prevent child `llm-scheduler --suspend-until-ready` calls from suspending outside Ralph's all-providers-exhausted decision)
* `LLM_TOOLS_RALPH_ROBIN_SELECTED_PROVIDER` (internal/inherited context: provider selected by Ralph for the current child run)
* `LLM_TOOLS_RALPH_ROBIN_PROVIDERS` (internal/inherited context: comma-separated Ralph rotation for the current child run)
* `LLM_TOOLS_RALPH_ROBIN_ALLOW_SUSPEND` (internal/test bypass for the inherited Ralph suspend guard)

Document any new user-facing or test-facing variable here and in `README.md` when appropriate.

## Test strategy

Prefer deterministic fixture tests over live provider calls. Tests must not require real Codex, Claude, Copilot, credentials, network access, or the user's actual home directory.

When changing behavior:

1. Add or update the narrowest fixture assertion.
2. Run a targeted command for the changed path.
3. Run `python -m pytest -q`.
4. Run coverage and require at least 85% total coverage: `coverage run -m pytest && coverage combine && coverage report --fail-under=85`.
5. Update `README.md` for user-visible changes.

Do not consider work done, even for small changes, unless the coverage gate has run and passed at `--fail-under=85`. If `coverage` is not installed in the active interpreter, use a temporary virtual environment or otherwise report the dependency/environment blocker explicitly.

## Common failures

* `KeyError`, `TypeError`, or `ValueError`: optional JSON or estimator state bug.
* Empty table cells: unavailable-provider path or remaining-time formatting bug.
* Column shifts: header/rule/value width mismatch.
* Copilot unexpectedly unavailable: PTY capture, timeout, trust prompt, footer regex, or auth state.
* Copilot values appear when footer is missing: unavailable JSON/table handling bug.
* Codex Spark missing: normalization or visibility filtering bug.
* JSON/table mismatch: normalization was bypassed or provider render paths diverged.
* Overconfident `Remaining Time`: estimator staleness/trend checks too loose.

## Done criteria

A change is complete only when:

* `./llm-usage --json` emits valid JSON.
* `./llm-usage --show-source --show-remaining-time` has aligned columns and no empty cells.
* `python -m pytest -q` passes.
* `coverage run -m pytest && coverage combine && coverage report --fail-under=85` passes with total coverage at or above 85%; this is mandatory for completion.
* Missing-provider and timeout paths degrade gracefully.
* Table, JSON, README, and tests are consistent for any user-visible change.
* Generated files such as `llm-usage.log` are not committed.
