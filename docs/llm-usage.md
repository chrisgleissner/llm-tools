# llm-usage

Remaining capacity for every provider, one row per scope. Back to the [README](../README.md).

Use `llm-usage` before starting work, in status lines, or in scripts that need a compact view of local LLM capacity.

```bash
llm-usage
llm-usage --json
llm-usage --watch 60
llm-usage --statusline
```

Default output shows all supported providers, with one row per capacity scope:

```text
LLM Usage · 13:03

Bars: quota rows █ available · ░ spent   ·   $ rows █ spent · ░ budget left
Guidance: 5h rows forecast runout; weekly/monthly/budget rows compare remaining quota to time left.
          $ rows show spend as a share of the overall monthly budget (green low · red at/over).
          ✓ lasts until reset · ! empty before reset · × empty · ↑ headroom · = on pace · ↓ conserve

Provider   Model     Ready   Scope     Remaining         Guidance              Resets in
────────   ───────   ─────   ───────   ───────────────   ───────────────────   ──────────
Codex                yes     5h        90% █████████░    ✓ lasts until reset   4h 34m
                             weekly    34% ███░░░░░░░    ↓ conserve            5d 2h
           Spark             5h       100% ██████████    ✓ lasts until reset   4h 34m
                             weekly    96% █████████░    ↑ headroom            6d 1h

Claude               no      5h         0% ░░░░░░░░░░    × empty               36m
                             weekly    91% █████████░    ↑ headroom            5d
           Sonnet            weekly   100% ██████████    ↑ headroom            5d

Copilot              yes     monthly   36% ████░░░░░░    ↓ conserve            17d 11h
                             spend     $0.0 ░░░░░░░░░░    0% of $50

Kilo                 yes     spend    $12.4 ██░░░░░░░░    24.8% of $50

OpenCode             yes     spend     $4.3 █░░░░░░░░░    8.5% of $50

Budget               yes     monthly  $16.7 ███░░░░░░░    33.5% of $50          14d 17h
```

Cost is shown like quota: the amount sits on the left, followed by a bar — never a right-aligned `spent $X`. A `spend` row (distinct from a funded `balance`) reports money already spent this cycle. With an overall monthly budget configured, its bar fills with how much of that budget the spend consumes (green low, red at/over) and the trailing `Budget` row totals every provider's spend against the cap; without a budget the rows show just the amount and the total appears as a plain `Total` row. The `Total`/`Budget` row only sums spend that carries a known bounded cycle (month-to-date), so a long-running install doesn't inflate the cross-provider total.

The `Model` column only appears when a provider reports model-specific limits (e.g. Codex's `Spark`, Claude's per-model weekly). These sub-rows are informational — scheduling always gates on the provider's aggregate scopes.

## Copilot

Copilot reports two extras beyond the usual quota: a `monthly` row for your included premium-request allowance, and a `spend` row for add-on usage billed beyond it. The `monthly` figure comes from the Copilot CLI footer when it shows one. Newer CLI releases do not, so it is then read from GitHub's Copilot quota endpoint (`/copilot_internal/user`), with the `premium_request/usage` billing sum as the fallback. The `spend` figure is summed from GitHub billing usage. The GitHub reads use a token already on your machine (`COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth token`) — no Copilot-specific credential. With no token the rows are simply omitted; they never affect `Ready`.

**Pay-as-you-go readiness.** Copilot keeps working past its included allowance via pay-as-you-go, up to the spending limit in your GitHub billing settings. GitHub does not expose that limit, so once the allowance is spent (`monthly` at 0%) `llm-usage` keeps Copilot `Ready` when overage is **funded** — either a limit you declare (`[copilot] monthly_spend_limit`, or `LLM_USAGE_COPILOT_SPEND_LIMIT`) or auto-detected when GitHub billing already shows overage being charged this month. When funded, the exhausted allowance row shows `pay-as-you-go` in the `Guidance` column instead of a misleading runout hint. (The CLI-footer parsing and GitHub-API fallback are detailed in [AGENTS.md](../AGENTS.md).)

## Table Columns

| Column      | Meaning                                                                                                                                            |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Model`     | The specific model a row reports on (e.g. `Spark`, `Sonnet`). Blank for a provider's aggregate rows. Only shown when model-specific data exists.   |
| `Ready`     | `yes` means every blocking scope for that provider has usable capacity now. `no` means at least one scope must reset or recover.                   |
| `Scope`     | The capacity measure, such as `5h`, `weekly`, `monthly`, `balance`, `budget`, or `spend` (money spent this cycle).                                  |
| `Remaining` | Remaining percentage, a `$` spend (with a bar when `[budget]` is set), a balance, or an unmetered state such as `byok`, `local`, or `ungated`.      |
| `Guidance`  | For `5h`, whether current burn should last until reset. For weekly/monthly/budget scopes, pace vs. a linear target. For `$` rows, share of budget. |
| `Resets in` | Relative reset time. Blank when the scope does not reset (`spend`/`balance`/`ungated`).                                                             |

Empty cells are intentional: a cell is left blank when it has nothing to report (a non-resetting scope, a window with no runout forecast). Only a real read failure is called out, as `unavailable`.

Set an overall monthly spend budget in `[budget]` (or the `LLM_USAGE_MONTHLY_BUDGET` / `LLM_USAGE_BUDGET_CURRENCY` env overrides) to turn every `spend` figure into a coloured progress bar against that cap, plus the `Budget` total row.

To work with only some providers, list them in `[usage]` (or the comma-separated `LLM_USAGE_PROVIDERS` env override, which wins over the file). The list applies to all tools:

- `llm-usage` reads and shows only the listed providers. Unlisted providers report `available:false` with `reason:"hidden"` in `--json`; the JSON top-level keys stay the same.
- `ralph-robin` drops rotation entries (from `--providers`, `--routes`, or the config) whose launch CLI is unlisted, prints a warning naming them, and exits with status 2 if none are left.
- `llm-scheduler` exits with status 2 when `--provider` (or `[scheduler].provider`) is unlisted.

Routes are matched by their launch CLI, so a Kilo route whose capacity is delegated to z.ai needs `kilo` in the list. An unknown provider name or an empty list is an error. Remove the key to enable every provider again.

```toml
[usage]
providers = ["claude", "codex"]
```

## `llm-usage` Options

| Option                   | Purpose                                                                                                 |
| ------------------------ | ------------------------------------------------------------------------------------------------------- |
| `-j`, `--json`           | Print stable JSON with `generated_at`, `codex`, `claude`, `copilot`, `kilo`, `opencode`, `minimax` and `zai`. A `routes` key is added only when a route is configured. |
| `-w`, `--watch SECONDS`  | Refresh continuously.                                                                                   |
| `-C`, `--show-copilot-credits` | Include Copilot AI credits when parseable.                                                        |
| `-S`, `--show-source`    | Show where each usage row came from.                                                                    |
| `-s`, `--hide-source`    | Hide the source column. This is the default.                                                            |
| `-R`, `--show-remaining-time` | Show burn-time estimates.                                                                          |
| `-r`, `--hide-remaining-time` | Hide burn-time estimates. This is the default.                                                    |
| `-D`, `--show-daily-budget` | Show the `Guidance` column. This is the default.                                                     |
| `-d`, `--hide-daily-budget` | Hide the `Guidance` column.                                                                          |
| `-K`, `--show-codex-spark` | Show Codex Spark rows.                                                                                |
| `-k`, `--hide-codex-spark` | Hide Codex Spark rows.                                                                                |
| `-M`, `--copilot-monthly-reset-offset-days DAYS` | Day offset from month start for Copilot monthly reset.                         |
| `-p`, `--provider-parallelism N` | Number of provider readers to run concurrently. Default: CPU cores; env: `LLM_USAGE_PROVIDER_PARALLELISM`. |
| `-t`, `--statusline`     | Read Claude Code statusline JSON from stdin and cache it.                                               |
| `-l`, `--log-only`       | Sample providers and append to the usage log without printing a table.                                  |
| `-n`, `--no-header`      | Omit the table header.                                                                                  |
| `--no-service`           | Read providers directly instead of using the local `llm-usage` service.                                 |
| `--service-install`      | Install and start the continuous sampler with the native user service manager.                          |
| `--service-uninstall`    | Stop and remove the installed continuous sampler.                                                       |
| `--service-start`        | Start the installed continuous sampler.                                                                |
| `--service-stop`         | Stop the installed continuous sampler.                                                                 |
| `--service-status`       | Show whether the local sampler is running.                                                             |
| `--service-run`          | Run the sampler in the foreground, useful for supervisors or debugging.                                 |
| `--service-interval SECONDS` | Continuous sampler refresh interval; default: `60`.                                                |
| `-h`, `--help`           | Show help.                                                                                              |


## Logs and Cache

Runtime data lives under `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools`:

```text
llm-tools/llm-usage/                 Usage caches and llm-usage.log
llm-tools/llm-usage/service/         Local service latest snapshot, history JSONL, logs
llm-tools/llm-scheduler/logs/        Per-run scheduler logs
llm-tools/ralph-robin/               Ralph state and logs
```

Each scheduler run directory contains `run.log`, `events.jsonl`, `prompt.txt`, and `attempt-N.out` / `attempt-N.status`. The scheduler logs arguments, prompt source and SHA-256, usage snapshots, wait decisions, the command plan, output, exit code, and final status. Convenience symlinks point at the latest runs:

```text
~/.cache/llm-tools/llm-scheduler/logs/latest
~/.cache/llm-tools/llm-scheduler/logs/latest-claude
~/.cache/llm-tools/llm-scheduler/logs/latest-codex
```

## Data Sources

`llm-tools` reads local provider state where possible, and **actively refreshes** providers when their samples expire — it never just echoes a stale session log. Each reader asks the provider for current numbers and only falls back if that fails:

| Provider       | Active refresh → fallback                                                   |
| -------------- | --------------------------------------------------------------------------- |
| Codex          | `codex app-server` (live, turn-free) → last cached payload → local session JSONL |
| Claude Code    | OAuth usage API (auto-refreshing the token) → API cache → statusline cache → project JSONL |
| GitHub Copilot | GitHub quota and billing APIs (about 1 s) → background PTY footer capture → cached capture |
| Kilo / MiniMax / OpenCode | Kilo's local database (month-to-date spend) → `kilo stats` / `mmx quota show` / `opencode stats` → environment variables |
| Z.ai           | Z.ai quota API (`/api/monitor/usage/quota/limit`) → environment variables |

Two sources are read before the slower CLI because they answer in about a second and report the same figure:

- **Copilot.** Copilot CLI 1.0.57 and later no longer print the monthly figure, so driving its terminal footer always waited out `LLM_USAGE_COPILOT_TIMEOUT` (10 s) before falling back to the GitHub quota endpoint. When a GitHub token is available, the quota endpoint is read first and the footer capture runs only if it has no answer. Test fixtures (`LLM_USAGE_COPILOT_CAPTURE_TEXT`, `LLM_USAGE_COPILOT_CAPTURE_CMD`) keep the footer first.
- **Kilo.** `kilo stats` takes about 6 s to start. Month-to-date spend comes from Kilo's local database (`~/.local/share/kilo/kilo.db`), which already replaced the CLI's figure when it was readable. When the row is a plain spend row (no configured balance or budget), the database answer is used and the CLI is not run. The currency symbol is `$`, as `kilo stats` prints it, unless `LLM_USAGE_KILO_CURRENCY` is set. The row's source reads `kilo db`.

A provider only reports `stale-usage` if it cannot be refreshed for a known authentication or CLI-startup reason (e.g. Codex `not-authenticated` or `missing-cli`). When the CLI is installed and signed in, you always see live data. `llm-usage` reads providers concurrently — configure fan-out with `--provider-parallelism` or `LLM_USAGE_PROVIDER_PARALLELISM` (default: CPU cores).

Claude usage reads share a sample for up to 60 seconds (`LLM_USAGE_LOCAL_SNAPSHOT_MAX_AGE` can shorten this), so a watch window and the sampler do not duplicate requests. Concurrent readers coordinate through a lock in the usage cache directory. On HTTP 429, readers share a cooldown of at least 60 seconds, including when Anthropic returns `Retry-After: 0`. Longer server delays are honoured without keeping the dashboard waiting; `LLM_USAGE_LIVE_FETCH_RETRY_MAX_DELAY` caps only an immediate retry's wait. During cooldown, an API sample may be shown for up to `LLM_USAGE_CLAUDE_RATE_LIMIT_CACHE_MAX_AGE` seconds (default 300); after that Claude reports `unavailable` with reason `rate-limited` until a live read succeeds. This does not mean the Claude sessions themselves have exhausted their quota, and no session restart or login is needed.

### First run and slow providers

All visible providers are read at the same time, so a cold run takes as long as the slowest provider, not the sum. On an interactive terminal the table does not wait for that provider:

- After `LLM_USAGE_FIRST_TABLE_WAIT` seconds (default `3`; `0` waits for every provider), the table is drawn with the providers that have answered. A provider still being read shows `reading…` in the Remaining column, with no Ready, Guidance or Resets value, and the `Total` row is left out because it would be partial.
- The spinner on stderr names the providers still being read.
- When they finish, the table is redrawn in place over the early one. If the early table would not fit the terminal height, there is nothing to redraw over, so the table waits for every provider instead.
- `--json`, pipes and `--watch` are unchanged: they wait for every provider and never print `reading…`.

Measured on this development machine with real Claude, Codex, Copilot, Kilo and OpenCode installed, empty caches and no service running (`LLM_USAGE_PROVIDERS` unset, so all seven providers are read), three runs each on a pseudo-terminal, at a load average of about 25 from other work:

| | Before | After |
| --- | --- | --- |
| First table | 17.6 to 18.9 s | 3.2 to 3.8 s |
| Complete table | 17.6 to 18.9 s | 5.5 to 8.5 s |

On a quieter machine the same code took 13.0 to 14.5 s before the change. About 5 s of the before figure was the ephemeral sampler described under Local Service.

Per-provider read time on an idle machine, before: Copilot 12.7 s, Kilo 6.5 s, OpenCode 4.6 s, Codex 1.4 s, MiniMax 1.1 s, Claude 0.3 s. After: Copilot 2.3 s, Kilo 0.01 s, OpenCode 4.4 s, Codex 2.4 s. OpenCode is now the slowest read because `opencode stats` starts a full runtime and has no local database to read instead.

### Local Service

A one-shot `llm-usage` first asks the local Unix-socket service for a snapshot. When a continuous service is running, the answer is immediate. When none is running:

- on a terminal, `llm-usage` reads the providers itself, with the spinner and the early table described above;
- when piped or with `--json`, it starts the sampler ephemerally, reads one snapshot, and shuts it down, falling back to direct reads if the service cannot start.

A sampler started only for one terminal run could not beat a direct read: it reads the same providers, shows no progress, and its first sample took longer than the five-second socket wait, so the run waited and then read directly anyway. For instant reports and continuous burn-down history, install the sampler as a native user service:

```bash
llm-usage --service-install
llm-usage --service-status
llm-usage --service-uninstall
```

On Linux this writes a `systemd --user` unit; on macOS a launchd LaunchAgent. The service is local-only: a Unix-domain socket under `${XDG_RUNTIME_DIR:-/tmp/llm-tools-$UID}`, `latest.json` and `history.jsonl` under the cache dir, and no HTTP listener, database, telemetry, or extra dependency. The socket directory must be a real directory you own with mode 0700 (a symlink or a directory another user created first is refused, and `llm-usage` then reads providers directly); the socket itself is mode 0600, and on Linux both sides check the peer UID with `SO_PEERCRED` and ignore a peer that is not you. Use `--no-service` for an explicit direct read.

### GitHub Copilot capture

The Copilot footer only shows plan/session usage when the `quota` and `ai-used` status-line items are enabled. These are off on a fresh install, so before each capture `llm-usage` enables them in `${COPILOT_HOME:-~/.copilot}/settings.json` (all other settings preserved). Set `LLM_USAGE_COPILOT_NO_SETTINGS_WRITE=1` to skip that write.

Capture is cached with `LLM_USAGE_COPILOT_CACHE_TTL` (default `300`; `0` forces synchronous capture) because the PTY footer capture is slow and occasionally flaky — a refresh that can't complete in its budget shows the most recent figure and refreshes in the background. To override the monthly allowance denominator, set `LLM_USAGE_COPILOT_PLAN` (e.g. `pro_plus`) or pin `LLM_USAGE_COPILOT_MONTHLY_ALLOWANCE`. The full set of Copilot knobs is catalogued in [AGENTS.md](../AGENTS.md).
