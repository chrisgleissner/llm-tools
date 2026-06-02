# llm-tools

[![Build](https://github.com/chrisgleissner/llm-tools/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/chrisgleissner/llm-tools/actions/workflows/test.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)](https://github.com/chrisgleissner/llm-tools/releases)

Small Bash tools for local LLM CLI usage:

- `llm-usage` shows remaining local usage for Codex, Claude Code, and GitHub Copilot.
- `llm-scheduler` waits until one selected CLI appears usable again, then submits one prompt.

The tools share provider detection code in `lib/llm-common.sh`; `llm-scheduler` does not reimplement the `llm-usage` parsers.

## Install

```bash
install -m 755 llm-usage ~/.local/bin/llm-usage
install -m 755 llm-scheduler ~/.local/bin/llm-scheduler
install -d ~/.local/bin/lib
install -m 644 lib/llm-common.sh ~/.local/bin/lib/llm-common.sh
command -v llm-usage
command -v llm-scheduler
```

If you keep the scripts in this repository, run them directly with `./llm-usage` and `./llm-scheduler`. If you copy them elsewhere, keep `lib/llm-common.sh` beside the scripts under a `lib` directory.

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
- `--retry-delays LIST` defaults to `60,180,600`; `--no-retry` disables retries.
- `--cwd DIR` sets the target CLI working directory.
- `--fresh` launches a fresh foreground CLI process and is the default.
- `--tmux SESSION[:WINDOW]` runs through tmux, creating the session/window when practical.
- `--command-template TEMPLATE` overrides CLI syntax. Placeholders are `{prompt}`, `{prompt_file}`, and `{cwd}`. The template is tokenized with Python `shlex`; it is not evaluated by a shell.
- `--auto-confirm` is enabled by default and only sends Return for recognised safe trust prompts. `--no-auto-confirm` disables it.
- `--log-dir DIR` defaults to `${XDG_CACHE_HOME:-$HOME/.cache}/llm-scheduler/logs`.
- `--dry-run` resolves usage state, timing, command plan, and logs without submitting.
- `--wake` enables best-effort wake scheduling.
- `--suspend-until-ready` arms a transient user systemd timer with `WakeSystem=true` for the next reset/not-before time, prints a brief confirmation showing the wake time, tool, model source, prompt, and working directory, suspends the machine, and runs `llm-scheduler` again after wake. This is useful when you want the desktop to sleep until a provider window resets instead of keeping a polling process active.
- `--wake-test` prints wake capability diagnostics without scheduling work.

Example for a Claude 5-hour reset:

```bash
llm-scheduler --tool claude --window 5h --prompt-file task.md --suspend-until-ready
```

Default provider adapters:

- Codex: `codex exec -C <cwd> <prompt>`
- Claude Code: `claude --print <prompt>` with the process working directory set to `--cwd`
- GitHub Copilot: `copilot -C <cwd> --prompt <prompt>`

Use `--command-template` if an installed CLI changes syntax or you use a wrapper.

## Logs

`llm-scheduler` creates a per-run directory under `${XDG_CACHE_HOME:-$HOME/.cache}/llm-scheduler/logs` by default. It uses restrictive permissions where possible and writes:

- `run.log` for human-readable audit output.
- `events.jsonl` for machine-readable events.
- `prompt.txt` containing the full prompt.
- `attempt-N.out` and `attempt-N.status` for CLI output and exit status.

The scheduler logs normalized arguments, prompt source, prompt SHA-256, full prompt content, usage snapshots, wait decisions, command plan, output, exit code, retry delays, and final status.

## Data Sources

- Codex: local JSONL under `~/.codex/sessions`.
- Claude Code: OAuth usage API/cache, statusline cache, then local project JSONL fallback.
- GitHub Copilot: local Copilot CLI footer captured through a bounded PTY helper.

## Requirements

- Bash, `jq`, `curl`, GNU coreutils, `find`, `sort`, `tail`, `date`, `sha256sum`, `python3`.
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
- Missing or inconclusive provider data degrades to `-`, `unknown`, or `unavailable`; the scheduler polls conservatively instead of guessing.
- Provider local data formats and CLI syntax can change.
- Copilot AI credits are parsed when requested by `llm-usage`, but scheduler gating uses monthly remaining usage unless later configured otherwise.

## Tests

Run:

```bash
./llm-usage-tests.sh
```

The tests use fixtures and mock commands; they do not require real Codex, Claude, Copilot, credentials, network access, or the user's real home directory.

## License

Apache License 2.0.
