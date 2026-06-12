# llm-tools

[![Build](https://github.com/chrisgleissner/llm-tools/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/chrisgleissner/llm-tools/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/chrisgleissner/llm-tools/graph/badge.svg)](https://codecov.io/gh/chrisgleissner/llm-tools)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)](https://github.com/chrisgleissner/llm-tools/releases)

Small command-line tools for local LLM CLI usage:

- `llm-usage` shows remaining local usage for Codex, Claude Code, and GitHub Copilot.
- `llm-scheduler` waits until one selected CLI appears usable again, then submits one prompt.
- `ralph-robin` keeps using one configured CLI until its usage window is exhausted, then advances to the next configured CLI.

## Install

```bash
python -m pip install .
command -v llm-usage
command -v llm-scheduler
command -v ralph-robin
```

For an isolated install, use a virtual environment or `pipx install .` from the repository checkout. If you keep this repository checked out, you can also run the tools directly with `./llm-usage`, `./llm-scheduler`, and `./ralph-robin`.

## llm-usage

`llm-usage` prints a compact snapshot of remaining quota windows:

```bash
llm-usage
llm-usage --json
llm-usage --watch 60
llm-usage --show-copilot-credits
llm-usage --show-source
llm-usage --statusline
```

Example table:

```log
Tool             Window         Remaining    Remaining Time   Resets             Time to Reset
--------------   ------------   ----------   --------------   ----------------   ------------
Codex            5h             100%         -                2026-06-02 23:59   4h 58m
Codex            weekly         53%          -                2026-06-07 16:25   4d 21h
Claude           5h             29%          -                2026-06-02 23:20   4h 19m
Claude           weekly         58%          10m              2026-06-04 13:00   1d 17h
Copilot          monthly        79%          -                2026-07-01 00:00   28d 4h
```

Important options:

- `--json` prints machine-readable JSON with stable top-level keys: `generated_at`, `codex`, `claude`, `copilot`.
- `--watch/-w SECONDS` refreshes continuously.
- `--show-source` shows where each row was derived from.
- `--show-copilot-credits` includes Copilot AI credits when parseable.
- `--show-remaining-time` and `--hide-remaining-time` control burn-time estimation.
- `--show-codex-spark` and `--hide-codex-spark` control Codex Spark rows.
- `--statusline` reads Claude Code statusline JSON from stdin and caches it.

## llm-scheduler

`llm-scheduler` submits a prompt once the selected CLI has known remaining capacity above a threshold. It runs once and exits after success, terminal failure, or retry exhaustion.

In the default fresh mode on an interactive terminal, the provider CLI launches in its normal interactive form attached directly to that terminal — output, key input, window resizes, and Ctrl-C behave exactly as if you had run `claude`, `codex`, or `copilot` yourself. An ANSI-cleaned transcript is also written to the run directory (`attempt-N.out`).

Without a terminal (pipes, cron, systemd resume), or with `--headless`, `LLM_SCHEDULER_HEADLESS=1`, or `LLM_SCHEDULER_NO_STREAM=1`, the non-interactive provider form runs on a captured PTY instead and its output streams to stdout (suppressed under `LLM_SCHEDULER_NO_STREAM=1`). In tmux mode the output appears in the tmux pane instead.

```bash
llm-scheduler --tool codex --prompt-file task.md
llm-scheduler --tool claude --prompt "Continue the work in this repo until CI is green"
llm-scheduler --tool copilot --prompt-file task.md --retry-delays 60,180,600
llm-scheduler --tool codex --prompt-file task.md --at "23:05"
llm-scheduler --tool codex --prompt-file task.md --tmux llm-work
llm-scheduler --tool codex --prompt-file task.md --wake
llm-scheduler --tool claude --prompt-file task.md --window 5h --suspend-until-ready
llm-scheduler --tool codex --prompt-file task.md --dry-run
```

Required scheduler form:

```bash
llm-scheduler --tool codex|claude|copilot (--prompt TEXT | --prompt-file FILE) [options]
```

Scheduler options:

- `--at TIME` or `--not-before TIME` delays submission until a `date -d` compatible local time.
- `--window auto|5h|weekly|monthly` chooses usage windows. `auto` checks all known Codex/Claude limiting windows and Copilot monthly usage.
- `--min-remaining PERCENT` defaults to `1`.
- `--poll-interval SECONDS` defaults to `60`.
- `--max-unavailable-wait SECONDS` defaults to `900`. When usage data cannot be measured (no network, a transient API failure, or inconclusive/unsupported data — common right after resume-from-suspend), the scheduler keeps polling only up to this long, then launches optimistically and lets the tool's own rate-limit handling and `--retry-delays` take over. `0` waits forever. A known rate-limit with a real reset time is excluded and always waits for that reset.
- `--retry-delays LIST` defaults to `60,180,600`; `--no-retry` disables retries.
- `--cwd DIR` sets the target CLI working directory.
- `--fresh` launches a fresh foreground CLI process and is the default.
- `--headless` always uses the non-interactive provider command and captured PTY, even on a terminal.
- `--tmux SESSION[:WINDOW]` runs through tmux, creating the session/window when practical.
- `--command-template TEMPLATE` overrides CLI syntax. Placeholders are `{tool}`, `{prompt}`, `{prompt_file}`, and `{cwd}`. The template is tokenized with Python `shlex`; it is not evaluated by a shell.
- `--auto-confirm` is enabled by default and only sends Return for recognised safe trust prompts. `--no-auto-confirm` disables it.
- `--headless-idle-timeout SECONDS` defaults to `600` (`LLM_SCHEDULER_IDLE_TIMEOUT`). In headless fresh mode, abort the provider if no output progress is observed for this long; `0` disables the idle watchdog.
- `--headless-question-timeout SECONDS` defaults to `30` (`LLM_SCHEDULER_QUESTION_IDLE_TIMEOUT`). In headless fresh mode, abort if question-like output appears and then no further progress is observed; `0` disables this watchdog. Known blocking prompt UIs, such as spend-limit menus, abort immediately.
- `--log-dir DIR` defaults to `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-scheduler/logs`.
- `--run-dir DIR` writes or resumes a specific run directory. This is mainly useful for wrappers and scheduled resume invocations that should keep all logs in one predictable place.
- `--dry-run` resolves usage state, timing, command plan, and logs without submitting.
- `--wake` enables best-effort wake scheduling.
- `--suspend-until-ready` arms a transient user systemd timer with `WakeSystem=true` for the next reset/not-before time, prints a brief confirmation showing the wake time, tool, model source, prompt, and working directory, suspends the machine, and runs `llm-scheduler` again after wake. This is useful when you want the desktop to sleep until a provider window resets instead of keeping a polling process active.
- `--wake-test` prints wake capability diagnostics without scheduling work.

Example for a Claude 5-hour reset:

```bash
llm-scheduler --tool claude --window 5h --prompt-file task.md --suspend-until-ready
```

Default provider adapters, attached (interactive terminal):

- Codex: `codex -C <cwd> <prompt>`
- Claude Code: `claude --dangerously-skip-permissions <prompt>` with the process working directory set to `--cwd`
- GitHub Copilot: `copilot -C <cwd> -i <prompt>`

Default provider adapters, headless (no terminal, or `--headless`):

- Codex: `codex exec -C <cwd> <prompt>`
- Claude Code: `claude --dangerously-skip-permissions --print <prompt>` with the process working directory set to `--cwd`
- GitHub Copilot: `copilot -C <cwd> --prompt <prompt>`

The Claude adapter skips permission prompts so unattended runs cannot stall; use `--command-template 'claude --print {prompt}'` if you want Claude Code's normal permission checks instead.

Use `--command-template` if an installed CLI changes syntax or you use a wrapper.

## ralph-robin

`ralph-robin` is a small rotation wrapper around `llm-scheduler`. It checks the configured tools in order, keeps using the current tool while it is still usable, tries another usable or undetermined tool before sleeping, and delegates the actual prompt launch, retry, wake, and suspend behavior to `llm-scheduler`.

`ralph-robin` defaults to autonomous headless launches even from an interactive terminal. The scheduler uses the provider's non-interactive adapter when available, streams output to your terminal, and aborts/re-evaluates rotation if a provider stops making progress or presents an input prompt.

On an interactive terminal, Ralph status lines and common streamed provider patterns such as diffs, command/tool-call lines, warnings, and errors are highlighted with ANSI color; streamed provider text is preserved without injected labels such as `STDERR`, `DIFF`, or `CMD`. Colors are disabled for non-TTY output, `TERM=dumb`, `NO_COLOR`, or `LLM_USAGE_NO_COLOR`.

The default Ralph/scheduler palette uses the terminal default foreground plus green, blue, and teal ANSI colors chosen to remain readable on typical dark and light terminals. Override any role with `LLM_TOOLS_COLOR_<ROLE>` set to an ANSI SGR code, for example `LLM_TOOLS_COLOR_ERROR=1;34`. Supported roles are `BRAND`, `INFO`, `OK`, `WARN`, `ERROR`, `DIM`, `DIFF_ADD`, `DIFF_REMOVE`, `DIFF_HUNK`, `COMMAND`, `TOOL`, `STDERR`, and `HEADING`.

Ralph status lines also use compact UTF-8 symbols so state remains distinguishable without relying only on color. Override a symbol with `LLM_TOOLS_SYMBOL_<ROLE>`, for example `LLM_TOOLS_SYMBOL_ERROR=!`, or set `LLM_TOOLS_NO_SYMBOLS=1` to keep color only.

```bash
ralph-robin --prompt-file task.md
ralph-robin --prompt "Continue until tests pass"
ralph-robin --tools claude,codex,copilot --prompt-file task.md
ralph-robin --prompt-file task.md --tmux llm-work
ralph-robin --prompt-file task.md --dry-run
```

Default rotation:

```text
claude -> codex -> claude -> ...
```

Important options:

- `--tools LIST` sets the comma-separated rotation. Values are `claude`, `codex`, and `copilot`.
- `--prompt TEXT` and `--prompt-file FILE` match `llm-scheduler`.
- `--window`, `--min-remaining`, `--poll-interval`, `--max-unavailable-wait`, `--retry-delays`, `--cwd`, `--fresh`, `--headless`, `--tmux`, `--command-template`, `--auto-confirm`, `--no-auto-confirm`, `--headless-idle-timeout`, and `--headless-question-timeout` are passed through to `llm-scheduler`.
- `--state-file FILE` defaults to `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/state.json` and stores the current provider index.
- `--log-dir DIR` defaults to `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/logs`.

When every configured tool is known to be rate-limited, `ralph-robin` chooses the earliest real reset and invokes `llm-scheduler --suspend-until-ready` for that provider. If usage cannot be measured rather than being known exhausted, Ralph tries that provider path before suspending, and the scheduler's bounded unavailable wait behavior still applies.

Before launching the selected provider, Ralph prepends a short runtime context to the prompt. It names the selected provider, lists the latest usage decisions, and tells the child to evaluate handoff/session-window instructions against the selected provider rather than a stale provider-specific scheduling command already present in the prompt.

Provider processes launched by Ralph inherit `LLM_TOOLS_RALPH_ROBIN_ACTIVE=1`, `LLM_TOOLS_RALPH_ROBIN_SELECTED_TOOL`, and `LLM_TOOLS_RALPH_ROBIN_TOOLS`. If a child agent or script tries to run `llm-scheduler --suspend-until-ready` directly while that marker is present, the scheduler exits with status `75` instead of suspending. This keeps Ralph as the single rotation/suspend coordinator. `LLM_TOOLS_RALPH_ROBIN_ALLOW_SUSPEND=1` is reserved for explicit internal bypasses.

If a selected provider exits with a scheduler autonomy abort, `ralph-robin` skips that provider for the current invocation, re-checks usage for the remaining tools, and launches the next usable provider. If every configured provider blocks this way, it exits with status `75` and leaves the logs under the printed run directory.

## Logs and Cached Data

All tools keep their runtime data under one root, `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools`, with one subdirectory per tool:

- `llm-tools/llm-usage/` — Claude status/API caches (`claude-status.json`, `claude-usage-api.json`) and `llm-usage.log`, the usage-sample log that drives `Remaining Time` estimates.
- `llm-tools/llm-scheduler/logs/` — per-run scheduler log directories.
- `llm-tools/ralph-robin/` — rotation `state.json` and per-run `logs/`.

`llm-scheduler` creates a per-run directory under `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/llm-scheduler/logs` by default. It uses restrictive permissions where possible and writes:

- `run.log` for human-readable audit output.
- `events.jsonl` for machine-readable events.
- `prompt.txt` containing the full prompt.
- `attempt-N.out` and `attempt-N.status` for CLI output and exit status.

The scheduler logs normalized arguments, prompt source, prompt SHA-256, full prompt content, usage snapshots, wait decisions, command plan, output, exit code, retry delays, and final status.

For reconnecting from another shell, use the latest symlinks and attempt logs:

```bash
tail -f ~/.cache/llm-tools/llm-scheduler/logs/latest/run.log
tail -f ~/.cache/llm-tools/llm-scheduler/logs/latest/attempt-1.out
```

Provider-specific symlinks such as `latest-claude` and `latest-codex` point at the most recent scheduler run for that provider. `ralph-robin` writes its own run log under `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/logs` and places the child scheduler logs in a `scheduler/` subdirectory of each `ralph-robin` run.

## Data Sources

- Codex: local JSONL under `~/.codex/sessions`.
- Claude Code: OAuth usage API/cache, statusline cache, then local project JSONL fallback.
- GitHub Copilot: local Copilot CLI footer captured through a bounded PTY helper. Because the capture is slow, results are cached (`LLM_USAGE_COPILOT_CACHE_TTL`, default 300s) and refreshed by a detached background capture, so `llm-usage` never blocks on it; set `LLM_USAGE_COPILOT_CACHE_TTL=0` to force a synchronous capture.

## Requirements

- Python 3.11 or newer.
- Optional: `copilot` or `github-copilot` for Copilot usage capture.
- Optional: `tmux` for scheduler tmux mode.
- Optional: `systemd-run` or `rtcwake` for best-effort wake support.

## Wake Limitations

`llm-scheduler --wake` is best effort. It prefers a transient user `systemd-run` timer with `WakeSystem=true` when available and logs an `rtcwake` fallback command that the user can run manually if privileges are required.

`llm-scheduler --suspend-until-ready` uses the same systemd wake timer mechanism, but schedules a resumed scheduler invocation and then calls `systemctl suspend` after the timer is accepted. If the machine suspends later by itself, the already-armed timer can still wake it for the scheduled run.

Set `LLM_SCHEDULER_PRE_SUSPEND_CONFIRMATION_SECONDS` to change the default 5-second confirmation pause before `systemctl suspend`.

Wake from suspend depends on firmware/BIOS settings, motherboard RTC support, kernel support, systemd user timers, and power state. The tool does not modify BIOS/UEFI settings and does not silently require `sudo`.

Run diagnostics with:

```bash
llm-scheduler --wake-test
```

## Limitations

- These tools can only use local data and local authenticated CLIs.
- They are not official billing dashboards.
- Missing or inconclusive provider data degrades to `-`, `unknown`, or `unavailable`; the scheduler keeps polling but, rather than blocking forever, launches optimistically once `--max-unavailable-wait` is exceeded (a known rate-limit still waits for its real reset time).
- Provider local data formats and CLI syntax can change.
- Copilot AI credits are parsed when requested by `llm-usage`, but scheduler gating uses monthly remaining usage unless later configured otherwise.

## Tests

Run:

```bash
python -m pip install -e . pytest coverage
coverage run -m pytest
coverage combine
coverage report --fail-under=80
```

The tests use fixtures and mock commands; they do not require real Codex, Claude, Copilot, credentials, network access, or the user's real home directory. For manual end-to-end checks against installed providers, run the examples above without the test fixture environment.

## License

Apache License 2.0.
