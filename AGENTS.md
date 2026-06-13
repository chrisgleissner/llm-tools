# AGENTS.md

## Scope

This repo contains small Linux Python CLIs for Codex, Claude Code, and GitHub Copilot:

* `llm-usage` — show local usage/quota for each provider.
* `llm-scheduler` — submit a prompt to a provider CLI once usage data says it is usable (optionally waking/suspending around a window reset).
* `ralph-robin` — keep using one configured provider until it is exhausted, then rotate to the next provider and delegate launch/suspend behavior to `llm-scheduler`.
* `llm_tools/common.py` — shared helpers (provider readers, normalization, time/reset formatting, subprocess execution, usage decisions, PTY capture, wake diagnostics, and common CLI plumbing: argument validation, run-dir logging, prompt loading, argv/JSON conversion).
* Python modules: `llm_tools/usage.py`, `llm_tools/scheduler.py`, `llm_tools/ralph_robin.py`, `llm_tools/copilot_refresh.py`, and package marker `llm_tools/__init__.py`.
* Public direct-run command files: `llm-usage`, `llm-scheduler`, `ralph-robin`.
* Regression tests: `tests/` with pytest and fake provider commands.
* Test helpers: `tests/conftest.py`; main suites: `tests/test_contracts.py`, `tests/test_additional_paths.py`.
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

Keep these as dependency-light Python CLIs sharing one helper module: no daemon, server, database, telemetry, or broad provider SDK design unless explicitly requested. Shared logic belongs in `llm_tools/common.py`, not duplicated across CLIs.

## Fast checks

```bash
chmod +x llm-usage llm-scheduler ralph-robin
./llm-usage
./llm-usage --json
./llm-usage --show-source --show-remaining-time
./llm-usage --hide-remaining-time --show-source
./llm-usage --show-copilot-credits --show-source
./llm-usage --hide-codex-spark
./llm-scheduler --tool codex --prompt x --dry-run --command-template true
./llm-scheduler --wake-test
./ralph-robin --prompt x --dry-run --command-template true
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
* Keep JSON top-level keys stable: `generated_at`, `codex`, `claude`, `copilot`.
* Keep Copilot unavailable shape explicit: `available:false`, with `reason` when known.
* Keep option semantics stable: `--show-source`, `--hide-source`, `--show-remaining-time`, `--hide-remaining-time`, `--show-codex-spark`, `--hide-codex-spark`, `--show-copilot-credits`.
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

## Scheduler invariants

* `llm-scheduler` gates on the same `llm_tools.common` provider readers as `llm-usage`; tests inject usage via `LLM_SCHEDULER_USAGE_JSON` and the command via `--command-template`, never live providers.
* A `rate-limited` decision (a known window with a real reset epoch) must wait for that reset, not proceed early.
* An *undeterminable* decision (`unavailable`, `inconclusive-usage`, `unsupported-window`) must never block forever: bound the wait with `--max-unavailable-wait`, then launch optimistically. See `is_undetermined_reason`.
* `--window` must be valid for the tool (copilot: auto/monthly; codex/claude: auto/5h/weekly). Reject other combinations in `validate_args`.
* Treat a tool launch as needing retry on non-zero exit, or on a clean exit whose output clearly signals a provider rate-limit/overload. Keep `output_is_retryable` patterns specific so ordinary successful agent output is not re-submitted. The synthetic autonomy-abort status `75` is different: do not retry the same provider session inside `llm-scheduler`.
* Under `--wake`, arm at most one OS wake timer per distinct, far-enough target (`log_wake_plan` lead guard + `WAKE_ARMED_TARGET`); never one per poll iteration.
* Never log secrets; prompt copies live under the run dir with `600`/`700` perms.
* Fresh mode on an interactive terminal runs the provider CLI in its normal interactive form on a PTY wired directly to that terminal via `script(1)` (`resolve_attach_mode`, `ATTACHED=1`): output, stdin, resizes, and Ctrl-C must behave exactly as a direct CLI launch. Headless fresh mode (no TTY, `--headless`, `LLM_SCHEDULER_HEADLESS=1`, or `LLM_SCHEDULER_NO_STREAM=1`) keeps the non-interactive provider commands and streams the child output live to the scheduler's stdout (and through `ralph-robin` to the invoking terminal) unless `LLM_SCHEDULER_NO_STREAM=1`. Both paths write the ANSI-cleaned copy to `attempt-N.out`. Attached runs never retry on a clean exit or user cancel (130/143) and skip the rate-limit phrase grep, since interactive screen content can legitimately mention rate limits. Headless runs must abort with status `75` when a blocking prompt UI is detected, when question-like output stalls, or when there is no output progress past `LLM_SCHEDULER_IDLE_TIMEOUT`; `ralph-robin` must treat status `75` as a reason to re-evaluate rotation, not as a final failure after the first provider. Tests extract the run dir from the `logs written to` stdout line, never via `awk '{print $NF}'` over all lines.
* Ralph must prepend provider-aware runtime context before launching a selected provider. That context must identify the selected provider, list latest usage decisions, and override stale provider-specific handoff/scheduler instructions in the original prompt so Codex does not hand off merely because Claude is exhausted, and vice versa.

## Environment knobs

Important knobs that tests or users may rely on:

* `LLM_USAGE_NO_COLOR`
* `LLM_USAGE_SHOW_SOURCE`
* `LLM_USAGE_SHOW_REMAINING_TIME`
* `LLM_USAGE_SHOW_CODEX_SPARK`
* `LLM_USAGE_NOW_EPOCH`
* `LLM_USAGE_MAX_FILES`
* `LLM_USAGE_TAIL_LINES`
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
* `LLM_TOOLS_RALPH_ROBIN_SELECTED_TOOL` (internal/inherited context: provider selected by Ralph for the current child run)
* `LLM_TOOLS_RALPH_ROBIN_TOOLS` (internal/inherited context: comma-separated Ralph rotation for the current child run)
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
