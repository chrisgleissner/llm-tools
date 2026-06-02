# AGENTS.md

## Scope

This repo contains a small Linux Bash CLI for showing local usage for Codex, Claude Code, and GitHub Copilot.

* Main script: `llm-usage`
* Regression tests: `llm-usage-tests.sh`
* User docs: `README.md`
* Runtime log: `llm-usage.log` beside the script when writable
* Cache: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-usage`

Keep it a single-file CLI with no build step, daemon, server, database, package framework, telemetry, or broad provider SDK design unless explicitly requested.

## Fast checks

```bash
chmod +x llm-usage llm-usage-tests.sh
./llm-usage
./llm-usage --json
./llm-usage --show-source --show-remaining-time
./llm-usage --hide-remaining-time --show-source
./llm-usage --show-copilot-credits --show-source
./llm-usage --hide-codex-spark
./llm-usage-tests.sh
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
* `LLM_USAGE_COPILOT_CWD`
* `LLM_USAGE_COPILOT_CAPTURE_CWD`
* `LLM_USAGE_COPILOT_MONTHLY_RESET_OFFSET_DAYS`
* `LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS`

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
