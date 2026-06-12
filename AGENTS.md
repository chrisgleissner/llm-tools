# AGENTS.md

## Scope

This repo contains small Linux Bash CLIs for Codex, Claude Code, and GitHub Copilot:

* `llm-usage` — show local usage/quota for each provider.
* `llm-scheduler` — submit a prompt to a provider CLI once usage data says it is usable (optionally waking/suspending around a window reset).
* `ralph-robin` — keep using one configured provider until it is exhausted, then rotate to the next provider and delegate launch/suspend behavior to `llm-scheduler`.
* `lib/llm-common.sh` — shared helpers (provider readers, normalization, time/reset formatting, and common CLI plumbing: argument validation, run-dir logging, prompt loading, argv/JSON conversion) sourced by the CLIs.
* Regression tests: `llm-usage-tests.sh` (covers all CLIs).
* User docs: `README.md`
* Runtime data root: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools`, one subdirectory per tool. Legacy `~/.cache/llm-usage`, `~/.cache/llm-scheduler`, and `~/.cache/ralph-robin` dirs are auto-migrated by `migrate_legacy_cache_dirs` in `lib/llm-common.sh`.
* Usage cache and samples log: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-usage` (`claude-status.json`, `claude-usage-api.json`, `llm-usage.log`)
* Scheduler run logs: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-scheduler/logs`
* Ralph Robin run logs: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/logs`
* Ralph Robin state: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/state.json`

Keep these as dependency-light Bash CLIs sharing one helper library: no build step, daemon, server, database, package framework, telemetry, or broad provider SDK design unless explicitly requested. Shared logic belongs in `lib/llm-common.sh`, not duplicated across CLIs.

## Fast checks

```bash
chmod +x llm-usage llm-scheduler ralph-robin llm-usage-tests.sh
./llm-usage
./llm-usage --json
./llm-usage --show-source --show-remaining-time
./llm-usage --hide-remaining-time --show-source
./llm-usage --show-copilot-credits --show-source
./llm-usage --hide-codex-spark
./llm-scheduler --tool codex --prompt x --dry-run --command-template true
./llm-scheduler --wake-test
./ralph-robin --prompt x --dry-run --command-template true
./llm-usage-tests.sh
shellcheck -x llm-usage llm-scheduler ralph-robin lib/llm-common.sh   # must be clean at default severity
```

Statusline mode reads Claude statusline JSON from stdin:

```bash
printf '%s\n' '{"rate_limits":{"five_hour":{"used_percentage":10}}}' | ./llm-usage --statusline
```

## Implementation map

* CLI/setup: `usage`, `need`, argument parsing, `render_once`, watch dispatch
* Provider readers: `read_codex`, `read_claude_api`, `read_claude`, `read_copilot`
* Normalization: `normalize_codex`, `normalize_claude`, Copilot parse helpers
* JSON: `json_for_provider`, `json_for_copilot`, JSON branch in `render_once`
* Table rendering: `print_cell`, `print_value_row`, `print_row`, `print_unavailable_rows`, `print_codex_rows`, `print_copilot_rows`
* Remaining-time logic: `log_usage_sample`, `estimate_remaining_time_from_log`
* Time/reset formatting: `now_epoch`, `parse_epoch`, `fmt_reset`, `fmt_duration`, `time_until`

Prefer changing the smallest relevant function surface. Preserve existing function boundaries unless a helper clearly reduces duplication or risk.

## Hard invariants

* Keep `set -euo pipefail`.
* Quote variables and use `local` in functions.
* Missing data must degrade gracefully as `-`, `unknown`, or `unavailable`, never as empty cells or script failure.
* One provider failing must not block other provider rows.
* Table and JSON must agree on provider availability and values.
* Keep at least three visible spaces between table columns.
* Keep color disabled for non-TTY output, `TERM=dumb`, or `LLM_USAGE_NO_COLOR`.
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

* `llm-scheduler` gates on the same `lib/llm-common.sh` provider readers as `llm-usage`; tests inject usage via `LLM_SCHEDULER_USAGE_JSON` and the command via `--command-template`, never live providers.
* A `rate-limited` decision (a known window with a real reset epoch) must wait for that reset, not proceed early.
* An *undeterminable* decision (`unavailable`, `inconclusive-usage`, `unsupported-window`) must never block forever: bound the wait with `--max-unavailable-wait`, then launch optimistically. See `is_undetermined_reason`.
* `--window` must be valid for the tool (copilot: auto/monthly; codex/claude: auto/5h/weekly). Reject other combinations in `validate_args`.
* Treat a tool launch as needing retry on non-zero exit, or on a clean exit whose output clearly signals a provider rate-limit/overload. Keep `output_is_retryable` patterns specific so ordinary successful agent output is not re-submitted.
* Under `--wake`, arm at most one OS wake timer per distinct, far-enough target (`log_wake_plan` lead guard + `WAKE_ARMED_TARGET`); never one per poll iteration.
* Never log secrets; prompt copies live under the run dir with `600`/`700` perms.
* Fresh mode streams the child CLI output live to the scheduler's stdout (and through `ralph-robin` to the invoking terminal) unless `LLM_SCHEDULER_NO_STREAM=1`; the cleaned copy still goes to `attempt-N.out`. Tests extract the run dir from the `logs written to` stdout line, never via `awk '{print $NF}'` over all lines.

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
* `LLM_SCHEDULER_NO_STREAM` (disable live pass-through of the child CLI output to stdout in fresh mode)
* `LLM_SCHEDULER_USAGE_JSON` (test: inject a usage snapshot)
* `LLM_SCHEDULER_NO_ACTUAL_SUSPEND` (test: skip the real `systemctl suspend`)
* `LLM_SCHEDULER_PTY_TIMEOUT` (fresh-process launch timeout, seconds)
* `LLM_SCHEDULER_TMUX_TIMEOUT` (tmux completion timeout, seconds)
* `LLM_SCHEDULER_WAKE_MIN_LEAD` (min seconds before a target to bother arming an OS wake timer)

Document any new user-facing or test-facing variable here and in `README.md` when appropriate.

## Test strategy

Prefer deterministic fixture tests over live provider calls. Tests must not require real Codex, Claude, Copilot, credentials, network access, or the user's actual home directory.

When changing behavior:

1. Add or update the narrowest fixture assertion.
2. Run a targeted command for the changed path.
3. Run `./llm-usage-tests.sh`.
4. Update `README.md` for user-visible changes.

## Common failures

* `unbound variable`: strict-mode bug, often optional JSON or estimator state.
* Empty table cells: unavailable-provider path or remaining-time formatting bug.
* Column shifts: header/rule/value width mismatch.
* Copilot unexpectedly unavailable: PTY capture, timeout, trust prompt, footer regex, or auth state.
* Copilot values appear when footer is missing: unavailable JSON/table handling bug.
* Codex Spark missing: normalization or visibility filtering bug.
* JSON/table mismatch: normalization was bypassed or provider render paths diverged.
* Overconfident `Remaining Time`: estimator staleness/trend checks too loose.

## Done criteria

A change is complete only when:

* `./llm-usage --json | jq . >/dev/null` succeeds.
* `./llm-usage --show-source --show-remaining-time` has aligned columns and no empty cells.
* `./llm-usage-tests.sh` prints `ok`.
* Missing-provider and timeout paths degrade gracefully.
* Table, JSON, README, and tests are consistent for any user-visible change.
* Generated files such as `llm-usage.log` are not committed.
