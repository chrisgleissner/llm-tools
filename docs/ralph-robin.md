# ralph-robin and llm-sleep-soak

Back to the [README](../README.md).

Use `ralph-robin` when the task matters more than which provider runs it. It runs a [Ralph loop](https://venturebeat.com/technology/how-ralph-wiggum-went-from-the-simpsons-to-the-biggest-name-in-ai-right-now/): a persistent autonomous workflow that keeps going instead of stopping when one provider reaches a limit, stalls, or becomes temporarily unusable. That makes it useful for long-running coding, repair, hardening, documentation, and investigation tasks.

`ralph-robin` wraps `llm-scheduler` and rotates across configured providers. It can either keep using the current provider until exhausted, or spread work so provider limits burn down at a similar rate — **even burn-down is the default**. When it selects Claude Code, it renders Claude's `stream-json` output as readable stdout (assistant text, tool calls, and results appear live).

```bash
ralph-robin --prompt-file task.md
ralph-robin --prompt "Continue until tests pass"
ralph-robin --providers claude,codex,copilot,kilo,minimax --prompt-file task.md
ralph-robin --prompt-file task.md --tmux llm-work
ralph-robin --prompt-file task.md --dry-run
```

Example output:

```text
ralph-robin --prompt-file ~/dev/ralph.prompt.md
[16:22:47] ◆ ralph-robin: · logs: ~/.cache/llm-tools/ralph-robin/logs/20260613-162247-ralph-robin-r67_na63
[16:22:47] ◆ ralph-robin: · usage claude: usable (5h 22% left, weekly 85% left) | codex: usable (5h 99% left, weekly 35% left)
[16:22:47] ◆ ralph-robin: ✓ selected claude (even-burn)
[16:22:52 claude] I'll begin the RALPH loop iteration following FAST-PATH STARTUP. Let me start by establishing current state.
[16:22:54 claude] Tool call: Bash
```

Each relayed provider line is prefixed with `[time provider]` by default, so a quiet increment is distinguishable from a wedged one. Customize with `--prefix time,provider,usage` or disable with `--prefix none`.

## `ralph-robin` Options

| Option                                                              | Purpose                                                                                                                                                       |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-P`, `--providers LIST`                                            | Set comma-separated provider rotation. Values include `claude`, `codex`, `copilot`, `kilo`, `opencode`, and `minimax`. Default: `claude,codex,opencode`, or `[ralph].providers` from the config. Forces provider mode. |
| `--routes LIST`                                                     | Comma-separated route ids and/or bare provider names. Use it when one launch CLI serves several models, such as `kilo-minimax-m3` and `kilo-zai-glm-52`. See [Route Mode](./configuration.md#route-mode). |
| `-p`, `--prompt TEXT`                                               | Prompt text passed to the selected provider.                                                                                                                  |
| `-f`, `--prompt-file FILE`                                          | Prompt file passed to the selected provider.                                                                                                                  |
| `-s`, `--scope auto\|5h\|weekly\|monthly\|balance\|budget\|byok\|ungated` | Capacity scope to gate on. Default: `auto`.                                                                                                         |
| `-W`, `--window SCOPE`                                              | Deprecated alias for `--scope`.                                                                                                                              |
| `-m`, `--min-remaining PERCENT`                                     | Minimum remaining capacity required to launch. Default: `1`.                                                                                                  |
| `-i`, `--poll-interval SECONDS`                                     | Poll interval passed to `llm-scheduler`. Default: `60`.                                                                                                      |
| `-u`, `--max-unavailable-wait SECONDS`                              | Bound inconclusive usage waits before optimistic launch. Default: `900`; `0` waits forever.                                                                  |
| `-r`, `--retry-delays LIST`                                         | Retry delays. Default: `60,180,600`.                                                                                                                          |
| `-R`, `--no-retry`                                                  | Disable retries.                                                                                                                                              |
| `-e`, `--even-burn`                                                 | Spread work to burn provider quota down evenly. Enabled by default.                                                                                           |
| `-E`, `--no-even-burn`                                              | Keep using the current provider until it is exhausted.                                                                                                        |
| `-n`, `--max-iterations N`                                          | Stop after `N` successful increments. Default `0` means no iteration cap. Use `1` for single-shot.                                                            |
| `-D`, `--max-duration D`                                            | Stop once `D` of wall-clock time elapses, such as `24h`, `90m`, `30s`, or seconds. Default: `24h`. Use `0` to disable. Whichever limit is reached first wins. |
| `-I`, `--min-iteration-seconds N`                                   | Minimum runtime floor for successive increments. Default: `5`; `0` disables.                                                                                  |
| `-x`, `--prefix LIST`                                               | Comma-separated fields stamped on each relayed provider line. Fields: `time`, `provider`, `usage`. Default: `time,provider`. Use `none` or an empty value to disable. |
| `-X`, `--prefix-usage-interval S`                                   | Refresh interval in seconds for the cached `usage` prefix field. Default: `15`. Use `0` to refresh every line.                                                |
| `-C`, `--cwd DIR`                                                   | Set provider working directory.                                                                                                                               |
| `-F`, `--fresh`                                                     | Launch a fresh provider process through `llm-scheduler`.                                                                                                      |
| `-H`, `--headless`                                                  | Force non-interactive provider command and captured PTY.                                                                                                      |
| `-T`, `--tmux SESSION[:WINDOW]`                                     | Execute through tmux via `llm-scheduler`.                                                                                                                     |
| `-g`, `--command-template TEMPLATE`                                 | Override provider syntax. Supports `{provider}`, `{prompt}`, `{prompt_file}`, and `{cwd}`.                                                                        |
| `-y`, `--auto-confirm`                                              | Auto-confirm recognised safe trust prompts. This is the default.                                                                                              |
| `-Y`, `--no-auto-confirm`                                           | Disable safe auto-confirmation.                                                                                                                               |
| `-q`, `--headless-idle-timeout SECONDS`                             | Abort headless fresh mode after no output progress. Default: `600`. Use `0` to disable.                                                                       |
| `-Q`, `--headless-question-timeout SECONDS`                         | Abort headless fresh mode after question-like output stalls. Default: `30`. Use `0` to disable.                                                               |
| `-S`, `--state-file FILE`                                           | Store current provider index. Default: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/state.json`.                                                    |
| `-L`, `--log-dir DIR`                                               | Set Ralph log directory. Default: `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/ralph-robin/logs`.                                                               |
| `-k`, `--wake`                                                      | Pass best-effort wake scheduling to `llm-scheduler`.                                                                                                          |
| `-U`, `--suspend-until-ready`                                       | Suspend even for the selected provider's own wait gates.                                                                                                     |
| `--watchdog`                                                       | Arm a hardware watchdog across each machine suspend so a wedged resume reboots instead of hanging. See [Reliable sleep/wake](#reliable-sleepwake).            |
| `-d`, `--dry-run`                                                   | Resolve rotation and usage state without submitting.                                                                                                          |
| `-h`, `--help`                                                      | Show help.                                                                                                                                                    |

Scope, retry, launch, and headless-timeout options (`-s`, `-m`, `-i`, `-u`, `-r`, `-R`, `-C`, `-F`, `-H`, `-T`, `-g`, `-y`, `-Y`, `-q`, `-Q`) are passed straight through to `llm-scheduler`.

## Behavior

`ralph-robin` owns the full rotation loop: provider selection, retries, waiting, suspend decisions, and handoff between providers. It starts each increment in autonomous headless mode (even from an interactive terminal), re-evaluates capacity before each increment, and re-submits the same prompt so long-running work continues across provider boundaries.

**Provider selection.** By default ralph-robin uses even burn-down: among ready providers it prefers the one with the most pace-adjusted headroom in its long-period plan scopes (`weekly`, `monthly`, Kilo `budget`), scoring each by its most-constrained plan scope so a draining weekly hands over instead of running to the floor. The short `5h` window still gates usability but does not drive ranking. Providers with no rankable plan scope — such as an opaque prepaid subscription like MiniMax M3 via Kilo — take fair turns by least-completed count, so they are neither starved nor allowed to monopolise the loop. Use `--no-even-burn` to stay on one provider until it is exhausted.

**Blocking and recovery.** ralph-robin does not stop just because every provider is currently blocked — it waits or suspends until the rotation can recover. The loop ends only on a non-recoverable failure, a degenerate instant-success streak, `--max-duration`, or `--max-iterations`. If a provider's usage cannot be measured, Ralph tries it before suspending; if a provider hits a scheduler autonomy abort, Ralph skips it for the current invocation and tries the next.

## Reliable sleep/wake

An overnight Ralph run waits out reset windows by suspending the whole machine — safe only if the box reliably resumes, and only sensible if nothing else suspends it mid-run. Ralph handles both, portably:

* **Defers your OS auto-suspend.** For its whole run Ralph holds a logind `idle` inhibitor (`systemd-inhibit --what=idle`) so a desktop idle timer (KDE PowerDevil, GNOME, logind's `IdleAction`) cannot suspend the machine mid-iteration. An `idle` inhibitor does not block Ralph's own deliberate, RTC-armed suspends, so you do not have to change your auto-suspend settings.
* **Never suspends without an armed wake.** Ralph arms an RTC wake (`rtcwake -m no` when it can, otherwise a `systemd-run --user` `WakeSystem=true` timer) before every suspend. If it cannot arm one, it waits awake instead.
* **Verifies wakes by behaviour.** After resume it checks how far the wall clock landed from target; an unreliable wake latches Ralph to awake-only for the rest of that run rather than risk repeating a bad cycle.
* **Caps suspend churn** with a minimum awake interval and an optional per-run cap, and writes a durable fsync'd ledger so a wedged resume that forced a hard reset is reported on the next start.
* **`--watchdog` (opt-in)** arms a hardware watchdog across each suspend so a hung resume reboots the machine instead of hanging. This needs a `/dev/watchdog` whose timer keeps counting across S3; without one it is a logged no-op.

The suspend backend is feature-detected (systemd today) and degrades to an awake wait where the tools are missing. Tuning knobs (`LLM_TOOLS_NO_INHIBIT`, `LLM_TOOLS_SUSPEND_DRIFT_TOLERANCE`, `LLM_RALPH_MIN_AWAKE_SECONDS`, `LLM_RALPH_MAX_SUSPENDS`, `LLM_SCHEDULER_SUSPEND_MIN_LEAD`, `LLM_TOOLS_WATCHDOG_DEVICE`) are documented in [AGENTS.md](../AGENTS.md). Run `llm-scheduler --wake-test` to see what your host supports.

When all providers are blocked, Ralph sets an RTC wake for the earliest known reset across the rotation and resumes its own loop after wake. This differs from `llm-scheduler --suspend-until-ready`, which wakes into one selected provider — Ralph wakes back into cross-provider rotation.

## `llm-sleep-soak` — prove sleep/wake is reliable

Before trusting unattended overnight runs, soak-test the exact suspend/wake path on your hardware:

```bash
llm-sleep-soak --cycles 50 --period 90s        # 50 real suspend/wake cycles
llm-sleep-soak --cycles 20 --period 2m --watchdog --json
```

Each cycle suspends the machine, wakes it via the same verified RTC path Ralph uses, measures wake drift, scrapes the kernel log for resume errors, and records the cycle in the durable ledger. It prints a `PASS`/`FAIL` summary and exits non-zero if any cycle resumed late or logged an error — or if an earlier run left a cycle unfinished (the fingerprint of a past wedged resume).

This is a **real-hardware test**: it genuinely suspends the machine and cannot run in CI. `LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1` runs the whole loop in simulation (no real sleep) if you just want to see the flow.
