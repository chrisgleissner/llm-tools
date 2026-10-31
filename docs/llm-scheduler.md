# llm-scheduler

Back to the [README](../README.md).

Use `llm-scheduler` when you want one specific provider to run one specific prompt, but only after that provider has usable capacity. It is useful for delayed launches, rate-limit-aware retries, tmux launches, wake scheduling, and suspend-until-ready workflows.

```bash
llm-scheduler --provider codex --prompt-file task.md
llm-scheduler --provider claude --prompt "Continue the work in this repo until CI is green"
llm-scheduler --provider copilot --prompt-file task.md --retry-delays 60,180,600
```

Required form:

```bash
llm-scheduler --provider codex|claude|copilot|kilo|opencode|minimax (--prompt TEXT | --prompt-file FILE) [options]
```

Common examples:

```bash
llm-scheduler --provider codex --prompt-file task.md --at "23:05"          # run after a local time
llm-scheduler --provider codex --prompt-file task.md --tmux llm-work       # run inside tmux
llm-scheduler --provider codex --prompt-file task.md --wake                 # schedule a wake-up
llm-scheduler --provider claude --prompt-file task.md --scope 5h --suspend-until-ready
llm-scheduler --provider codex --prompt-file task.md --dry-run              # show the plan, don't launch
```

## Runtime Behavior

* In an interactive terminal, the provider is launched directly and output is written to `attempt-N.out`.
* In headless or non-terminal mode, the provider runs through a captured PTY.
* In tmux mode, the provider runs inside the requested tmux session or window.
* The scheduler exits after success, terminal failure, or retry exhaustion.

## `llm-scheduler` Options

| Option                                                              | Purpose                                                                                                                                            |
| ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-P`, `--provider PROVIDER`                                         | Provider: `codex`, `claude`, `copilot`, `kilo`, `opencode`, or `minimax`.                                                                          |
| `-M`, `--model MODEL`                                               | Pin the provider model. Overrides the config file policy.                                                                                          |
| `-p`, `--prompt TEXT`                                               | Prompt text.                                                                                                                                       |
| `-f`, `--prompt-file FILE`                                          | Read prompt from `FILE`, preserving content.                                                                                                       |
| `-a`, `--at TIME`                                                   | Delay launch until a `date -d` compatible local time.                                                                                              |
| `-a`, `--not-before TIME`                                           | Do not launch before a `date -d` compatible local time.                                                                                            |
| `-s`, `--scope auto\|5h\|weekly\|monthly\|balance\|budget\|byok\|ungated` | Capacity scope to gate on. Default: `auto`.                                                                                              |
| `-W`, `--window SCOPE`                                              | Deprecated alias for `--scope`.                                                                                                                    |
| `-m`, `--min-remaining PERCENT`                                     | Minimum remaining capacity required to launch. Default: `1`.                                                                                       |
| `-i`, `--poll-interval SECONDS`                                     | Usage polling interval. Default: `60`.                                                                                                             |
| `-u`, `--max-unavailable-wait SECONDS`                              | Maximum wait when usage cannot be measured. Default: `900`. Use `0` to wait forever. Known reset times still wait for reset.                       |
| `-r`, `--retry-delays LIST`                                         | Retry delays. Default: `60,180,600`.                                                                                                               |
| `-R`, `--no-retry`                                                  | Disable retries.                                                                                                                                   |
| `-C`, `--cwd DIR`                                                   | Set provider working directory.                                                                                                                    |
| `-F`, `--fresh`                                                     | Launch a fresh foreground provider process. This is the default.                                                                                   |
| `-H`, `--headless`                                                  | Force non-interactive provider command and captured PTY.                                                                                           |
| `-T`, `--tmux SESSION[:WINDOW]`                                     | Run through tmux.                                                                                                                                  |
| `-e`, `--command-template TEMPLATE`                                 | Override provider syntax. Supports `{provider}`, `{prompt}`, `{prompt_file}`, and `{cwd}`. Parsed with Python `shlex`, not a shell.                    |
| `-y`, `--auto-confirm`                                              | Auto-confirm recognised safe trust prompts. This is the default.                                                                                   |
| `-Y`, `--no-auto-confirm`                                           | Disable safe auto-confirmation.                                                                                                                    |
| `-I`, `--headless-idle-timeout SECONDS`                             | Abort headless fresh mode after no output progress. Default: `600`. Use `0` to disable.                                                            |
| `-Q`, `--headless-question-timeout SECONDS`                         | Abort headless fresh mode after question-like output stalls. Default: `30`. Use `0` to disable.                                                    |
| `-L`, `--log-dir DIR`                                               | Set scheduler log root.                                                                                                                            |
| `-O`, `--run-dir DIR`                                               | Write or resume a specific run directory.                                                                                                          |
| `-d`, `--dry-run`                                                   | Resolve usage, timing, command plan, and logs without launching.                                                                                   |
| `-k`, `--wake`                                                      | Enable best-effort wake scheduling.                                                                                                                |
| `-U`, `--suspend-until-ready`                                       | Schedule a resumed run, enable wake, suspend the machine, and continue after wake.                                                                 |
| `-x`, `--wake-test`                                                 | Print wake diagnostics without scheduling work.                                                                                                    |
| `-h`, `--help`                                                      | Show help.                                                                                                                                         |

## Default Provider Commands

| Provider       | Interactive                      | Headless                             |
| -------------- | -------------------------------- | ------------------------------------ |
| Codex          | `codex -C <cwd> <prompt>`        | `codex exec -C <cwd> <prompt>`       |
| Claude Code    | `claude <prompt>`                | `claude --print <prompt>`            |
| GitHub Copilot | `copilot -C <cwd> -i <prompt>`   | `copilot -C <cwd> --prompt <prompt>` |
| Kilo Code      | `kilo --prompt <prompt>`         | `kilo run --dir <cwd> <prompt>`      |
| OpenCode       | `opencode --prompt <prompt>`     | `opencode run --dir <cwd> <prompt>`  |
| MiniMax        | _refused (exit 2)_               | `mmx text chat --message <prompt>`   |
| Z.ai           | _launch via Kilo or OpenCode_    | _launch via Kilo or OpenCode_        |

Kilo Code and OpenCode accept `-m, --model <provider>/<model>`; the scheduler and Ralph inject this flag when the per-provider policy or route pins a model (e.g. `-m zai/glm-4.7`). No permission-bypassing flag is injected — whether a headless run may act without prompting is governed by each tool's own permission config. Interactive Kilo and OpenCode inherit the working directory from the launching process. MiniMax has no native engineering session, so an interactive launch exits with status 2; pass `--headless`. To override Claude Code's settings for one run, use `--command-template`, e.g. `claude --permission-mode plan --print {prompt}`.
