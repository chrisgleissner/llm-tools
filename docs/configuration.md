# Configuration

Config file, capacity scopes, routes, provider setup and appearance. Back to the [README](../README.md).

## Provider CLIs

`llm-tools` drives the official command-line clients for each provider. Install and authenticate the CLI for each provider you want to use.

| Provider       | CLI binary | Install                                                                                                                   |
| -------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------- |
| Claude Code    | `claude`   | [claude.com/product/claude-code](https://www.claude.com/product/claude-code) - `npm install -g @anthropic-ai/claude-code` |
| Codex          | `codex`    | [github.com/openai/codex](https://github.com/openai/codex) - `npm install -g @openai/codex`                               |
| GitHub Copilot | `copilot`  | [github.com/github/copilot-cli](https://github.com/github/copilot-cli) - `npm install -g @github/copilot`                 |
| Kilo Code      | `kilo`     | [kilo.ai](https://kilo.ai) - `npm install -g @kilocode/cli`                                                               |
| MiniMax        | `mmx`      | [platform.minimax.io](https://platform.minimax.io/) - `npm install -g mmx-cli`                                            |
| OpenCode       | `opencode` | [opencode.ai](https://opencode.ai/) - `npm install -g opencode-ai`                                                        |
| Z.ai (capacity)| _none_     | Capacity-only: zero-config — the key is read from Kilo's/OpenCode's `auth.json`; launch via Kilo (`zai/<model>`) — see [Z.ai](#zai-glm-via-kilo-or-opencode). |

You do not need every provider CLI installed.

* `llm-usage` reports unavailable providers as `unavailable` and still shows the rest.
* `llm-scheduler` only needs the provider selected with `--provider`.
* `ralph-robin` skips unavailable providers and rotates across the usable ones. Its default rotation is `claude,codex,opencode`; use `--providers` to change it.

## Capacity Scopes

`llm-tools` calls every quota-like constraint a **scope** — one capacity measure exposed by one provider. For example, Codex and Claude expose `5h` and `weekly` reset windows, while Kilo can expose a credit balance or monthly budget.

| Kind           | Resets? | Examples                             | Providers                       |
| -------------- | ------- | ------------------------------------ | ------------------------------- |
| `reset_window` | yes     | `5h`, `weekly`, `monthly`            | Codex, Claude, Copilot, MiniMax, Z.ai |
| `balance`      | no      | Kilo credit balance, GBP/USD/credits | Kilo, OpenCode                  |
| `budget`       | yes     | Monthly spend budget                 | Kilo, OpenCode                  |
| `ungated`      | n/a     | BYOK, local, ungated mode            | Kilo, OpenCode                  |
| `opaque`       | n/a     | Prepaid gateway subscription         | configured via routes           |

`llm-usage` shows one row per scope. `llm-scheduler` and `ralph-robin` can gate on a specific scope with `--scope`. `opaque` is capacity that exists but cannot be measured before launch (most commonly a prepaid gateway subscription); it is selected by a route — see [Route Mode](#route-mode).

Per-provider scope allow-lists:

| Provider       | Supported `--scope` values                     |
| -------------- | ---------------------------------------------- |
| Codex          | `auto`, `5h`, `weekly`                         |
| Claude Code    | `auto`, `5h`, `weekly`                         |
| MiniMax        | `auto`, `5h`, `weekly`                         |
| Z.ai           | `auto`, `5h`, `weekly`                         |
| GitHub Copilot | `auto`, `monthly`                              |
| Kilo Code      | `auto`, `balance`, `budget`, `byok`, `ungated` |
| OpenCode       | `auto`, `balance`, `budget`, `byok`, `ungated` |

## Configuration File

For shared settings across `llm-usage`, `llm-scheduler`, and `ralph-robin`, drop a TOML file at one of these locations (first match wins):

1. `$LLM_TOOLS_CONFIG` (explicit path)
2. `$XDG_CONFIG_HOME/llm-tools/config.toml`
3. `~/.config/llm-tools/config.toml`

A missing file is fine — the tools use their built-in defaults. **What wins when settings overlap:** built-in defaults < config file < CLI flags. Unknown sections or keys are rejected at load time so typos surface immediately.

The main thing the file sets is which model each provider runs, and what to do when that model's limit is used up:

```toml
# ~/.config/llm-tools/config.toml

[defaults]
# Order ralph-robin tries providers when --providers isn't given.
providers     = ["claude", "codex", "opencode"]
# Which capacity check to use: auto | 5h | weekly | monthly | balance | budget | byok | ungated
scope         = "auto"
# Minimum quota left before a provider is considered usable.
min_remaining = 1

[providers.claude]
model          = "sonnet"   # run `claude --model sonnet`; only run while Sonnet has capacity
# When Sonnet's limit is used up: false -> skip claude; true -> keep claude, let it pick another model
allow_fallback = false
#scope          = "weekly"   # optional per-provider override of [defaults]
#min_remaining  = 5

[providers.codex]
model          = "spark"
allow_fallback = false

[providers.opencode]
# Gate opencode on another provider's usage windows instead of its own — see capacity_provider below.
capacity_provider = "minimax"

[ralph]                                # ralph-robin-only settings (override [defaults])
providers      = ["claude", "codex", "kilo"]

[scheduler]                            # llm-scheduler-only settings (override [defaults])
provider       = "claude"

[usage]                                # providers enabled for all tools (omit to enable all)
providers      = ["claude", "codex"]
```

A complete template with every supported key (all commented out) ships at [config.example.toml](../config.example.toml). Copy it to one of the locations above and uncomment what you want.

**`model` / `allow_fallback`.** With `model` set, the tools call the provider with `--model NAME` and only run while that model has capacity. `allow_fallback = false` (default) treats the provider as unavailable once the model's limit is used up and moves to the next provider; `allow_fallback = true` keeps the provider but drops the model pin so its CLI picks another model. `--model NAME` on the command line overrides the file for a single run.

**`capacity_provider`.** Ties one provider's gating to another provider's usage windows (a single hop). The borrowing provider's own CLI is still launched — only the capacity reading is delegated. The motivating case is a CLI configured to run a different provider's model, such as OpenCode pointed at the MiniMax API, where OpenCode's own balance says nothing about whether a run will succeed. With `capacity_provider = "minimax"`, `ralph-robin` only routes to OpenCode while MiniMax has capacity, even-burns on MiniMax's remaining, and suspends until MiniMax's reset. Route mode's `delegate` policy is the successor to this setting (see below).

## Route Mode

By default `ralph-robin` rotates over **providers**, and each entry runs one model — the CLI's default, or the one pinned in `[providers.<name>]`. **Route mode** rotates over **routes** instead. A route binds a launch provider, a specific model, a capacity policy, and a cost policy into one schedulable unit — so a single CLI can serve several models in the same rotation (for example, one Kilo install running both MiniMax M3 and GLM 5.2). `--providers kilo` can only ever reach one Kilo model, because only a route carries a per-entry `model` pin.

### Turning it on

Set `[ralph].routes` in your config and plain `ralph-robin --prompt-file task.md` rotates over those routes. Pass `--routes a,b` to override the list for one run.

One gotcha: **`--providers` (`-P`) forces legacy provider mode and silently ignores your routes.** So `-P kilo` launches Kilo with its default model only, even when you have two Kilo routes configured.

```
# ✗ launches kilo with its default model only, ignoring [ralph].routes
ralph-robin --providers kilo --prompt-file task.md

# ✓ rotates both Kilo models, even-burning across whichever is usable
ralph-robin --routes kilo-minimax-m3,kilo-zai-glm-52 --prompt-file task.md
```

### Defining a route

```toml
[ralph]
routes = ["kilo-minimax-m3"]   # presence of this list enables route mode

[routes.kilo-minimax-m3]
provider       = "kilo"
model          = "kilo/minimax/minimax-m3"
allow_fallback = false

[routes.kilo-minimax-m3.capacity]
# How readiness is measured. One of:
#   provider        - read the provider's own snapshot (default)
#   provider_model  - model-aware (claude / codex have per-model buckets)
#   delegate        - launch this route's provider, read capacity from another
#   opaque          - capacity exists but cannot be measured before launch
#   ungated         - usable whenever the launch CLI is present
#   balance         - read the provider's balance scope
#   budget          - read the provider's budget scope
policy = "opaque"
scope  = "subscription"     # display name; defaults to "subscription"
label  = "MiniMax M3 via Kilo"

[routes.kilo-minimax-m3.cost]
# Display only; never affects readiness. One of:
#   included, fixed_subscription, metered_balance, metered_budget,
#   free, external, unknown
policy   = "fixed_subscription"
amount   = 20
currency = "USD"
period   = "monthly"
```

`llm-usage` renders this as:

```
Provider   Model       Ready   Scope          Remaining         Guidance              Resets in
Kilo       MiniMax M3  yes     subscription   prepaid USD20/mo   ✓ usable              -
```

`opaque` rows never show a percentage, balance, reset time, or progress bar. The `prepaid USD20/mo` text comes from the cost block; routes without a `fixed_subscription` cost render as `not metered`. The `delegate` policy replaces the legacy `capacity_provider` setting, and the selected `route_id` and launch provider are injected into the run's prompt so a handoff never stale-routes to the wrong provider.

### One CLI, several models

A route's `model` is what ralph-robin pins on the launch command, so two routes sharing a provider select different underlying models. **The pin must be an id from the CLI you launch, in `provider/model` form** (`kilo models` / `opencode models` list the exact ids). Two rules to respect:

- A bare model name is misparsed as a provider with an empty model and fails at launch with `Model not found: <name>/`.
- The `provider/` half is a credential namespace, and Kilo and OpenCode do **not** share it. Kilo's MiniMax path is `kilo/minimax/minimax-m3`; OpenCode's is `minimax-coding-plan/MiniMax-M3`. Cross-wiring them is not rejected up front — Kilo has no `minimax-coding-plan` credential, so it prints its banner and then **hangs forever** (no auth, no timeout) instead of erroring.

Here one Kilo install serves both models in a single even-burn rotation, each gated on its own real capacity:

```toml
[routes.kilo-minimax-m3]
provider = "kilo"
model    = "kilo/minimax/minimax-m3"
[routes.kilo-minimax-m3.capacity]
policy   = "opaque"       # entitlement lives behind the Kilo gateway; no pre-launch window
scope    = "subscription"

[routes.kilo-zai-glm-52]
provider = "kilo"
model    = "zai/glm-5.2"
[routes.kilo-zai-glm-52.capacity]
policy   = "delegate"     # gate on Z.ai's real 5h/weekly windows
provider = "zai"
```

A second Z.ai route, `kilo-zai-glm-47` for `zai/glm-4.7`, is defined the same way. Avoid a dot in the id: an unquoted dot in a TOML table name splits the key, so `4.7` becomes `47`.

### Mixing routes and bare providers

The route list also accepts **bare provider names**, which become implicit routes on their own CLI, gated on their own capacity. So you can burn down codex and claude alongside the Kilo routes without declaring `[routes.codex]` / `[routes.claude]`:

```
ralph-robin --routes codex,claude,kilo-minimax-m3,kilo-zai-glm-52 --prompt-file task.md
```

This rotates over four independent capacity pools and even-burns across whichever are usable.

## Provider Setup Details

Most providers only need their CLI installed and authenticated once. Kilo, MiniMax, and Z.ai also support environment-variable fallbacks, useful for CI and deterministic tests.

### Kilo Code

Kilo is driven primarily through its CLI, with environment variables as a CI/test fallback:

| Variable                           | Purpose                                                                   |
| ---------------------------------- | ------------------------------------------------------------------------- |
| `LLM_USAGE_KILO_MODE`              | `gateway` (default), `budget`, `byok`, `local`, or `ungated`.             |
| `LLM_USAGE_KILO_BALANCE`           | Remaining credit balance. Required for `balance` scope.                   |
| `LLM_USAGE_KILO_CURRENCY`          | Currency or unit label, such as `GBP`, `USD`, or `credits`.               |
| `LLM_USAGE_KILO_MIN_BALANCE`       | Minimum remaining balance required to consider Kilo usable. Default: `1`. |
| `LLM_USAGE_KILO_MONTHLY_BUDGET`    | Total monthly budget. Enables `budget` scope.                             |
| `LLM_USAGE_KILO_MONTHLY_SPENT`     | Amount already spent in this budget period.                               |
| `LLM_USAGE_KILO_MONTHLY_RESET_DAY` | Day of month the budget resets. Default: `1`.                             |

When `kilo` is on `PATH`, the tools try `kilo stats` (JSON or text) first and fall back to the variables above. The monetary `spend` row is an exact month-to-date sum from Kilo's local database rather than a rolling `--days N` approximation. With `--scope auto`, Kilo prefers `budget`, then `balance`, then `ungated`.

**Gateway-backed models (e.g. MiniMax M3 via Kilo).** When Kilo sells another provider's model through its gateway, the entitlement lives behind the Kilo gateway and cannot be measured before launch — model it as an `opaque` route, pinned to an id from Kilo's own catalogue (`kilo/minimax/minimax-m3`). See [Route Mode](#route-mode) for the full config; the legacy `capacity_provider` setting is for the *truthful* delegation case only.

### Z.ai (GLM via Kilo or OpenCode)

Z.ai is a capacity-only provider: there is no `zai` CLI, only the GLM family (`GLM-4.7`, `GLM-5.2`, …) served through Kilo (or OpenCode) via the `zai/<model>` id. `llm-usage` reads your Z.ai `5h`/`weekly` quota directly from the official monitoring API; `llm-scheduler` / `ralph-robin` launch the configured provider with `-m zai/<model>` through a route with `capacity.policy = "delegate"` and `provider = "zai"` (see [Route Mode](#route-mode)).

**Zero-config key discovery.** You do not configure a Z.ai key in `llm-tools`. When you authenticate Z.ai in Kilo (or OpenCode), the key is stored in that agent's owner-only `auth.json`; the reader discovers it there automatically, so adding a Z.ai account to Kilo lights up the dashboard row with no further setup. A bad or missing key reads as `not-authenticated` (rather than a generic `unavailable`), so "wrong key" is distinguishable from "API down". The environment variables below are an explicit override / hermetic-test path, not required.

| Variable                          | Purpose                                                            |
| --------------------------------- | ------------------------------------------------------------------ |
| `ZAI_API_KEY`                     | Bearer token override (takes precedence over the discovered key).  |
| `LLM_USAGE_ZAI_API_KEY`           | Same, but overrides `ZAI_API_KEY` (mainly for tests).              |
| `LLM_USAGE_ZAI_MODEL`             | Display-only GLM pin (e.g. `zai/glm-4.7`); does not affect gating. |
| `LLM_USAGE_ZAI_5H_PERCENT` / `..._RESET_EPOCH` | Hermetic fallback for the 5h window (percent `0..100`, epoch s/ms). |
| `LLM_USAGE_ZAI_WEEKLY_PERCENT` / `..._RESET_EPOCH` | Hermetic fallback for the weekly window.                     |
| `LLM_USAGE_ZAI_TIMEOUT`           | HTTP timeout in seconds. Default `10`.                             |

```bash
# Single GLM model on Kilo, gated on Z.ai's 5h quota. Needs this in the config:
#   [providers.kilo]
#   model             = "zai/glm-4.7"
#   capacity_provider = "zai"
llm-scheduler --provider kilo --prompt-file task.md --scope 5h

# Even-burn across two GLM models on Kilo, gated on Z.ai's real windows.
# Needs routes named kilo-zai-glm-47 and kilo-zai-glm-52, as defined above.
ralph-robin --routes kilo-zai-glm-47,kilo-zai-glm-52 --prompt-file task.md
```

Without `capacity_provider = "zai"`, `--scope 5h` is rejected for Kilo, which only accepts `auto`, `balance`, `budget`, `byok` and `ungated`.

Launching Z.ai directly via `--provider zai` is rejected — it has no CLI to run; launch Kilo or OpenCode through a route or `capacity_provider`. `llm-usage --json` emits a `zai` top-level key with the parsed `5h` and `weekly` scopes. (The API endpoints, payload classification, and China-mirror fallback are detailed in [AGENTS.md](../AGENTS.md).)

### MiniMax

MiniMax quota is read from the local `mmx` CLI (`mmx quota show --output json`), with environment variables as a fallback for tests:

| Variable                               | Purpose                                                            |
| -------------------------------------- | ------------------------------------------------------------------ |
| `LLM_USAGE_MINIMAX_5H_PERCENT`         | Remaining percentage for the 5h session window, from `0` to `100`. |
| `LLM_USAGE_MINIMAX_5H_RESET_EPOCH`     | Epoch seconds or milliseconds when the 5h window resets.           |
| `LLM_USAGE_MINIMAX_WEEKLY_PERCENT`     | Remaining percentage for the weekly window, from `0` to `100`.     |
| `LLM_USAGE_MINIMAX_WEEKLY_RESET_EPOCH` | Epoch seconds or milliseconds when the weekly window resets.       |
| `LLM_USAGE_MINIMAX_MODEL`              | `model_remains` row to read. Default: `general`.                   |
| `LLM_USAGE_MINIMAX_TIMEOUT`            | Timeout for `mmx quota show`, in seconds. Default: `10`.           |

MiniMax exposes the same `5h` and `weekly` reset windows as Claude Code and Codex, so it renders and gates identically. The row appears only when the `mmx` CLI is installed or MiniMax environment variables are set.

## Appearance and Output Customization

Override colors and symbols with environment variables:

```bash
LLM_TOOLS_COLOR_ERROR='1;34'     # one color role
LLM_TOOLS_SYMBOL_ERROR=!         # one symbol role
LLM_TOOLS_NO_SYMBOLS=1           # disable symbols, keep color
```

Color roles: `BRAND, INFO, OK, WARN, ERROR, DIM, DIFF_ADD, DIFF_REMOVE, DIFF_HUNK, COMMAND, TOOL, STDERR, HEADING`. Colors are disabled automatically for non-TTY output, `TERM=dumb`, `NO_COLOR`, or `LLM_USAGE_NO_COLOR`.

Ralph-launched provider processes inherit `LLM_TOOLS_RALPH_ROBIN_ACTIVE=1`, `..._SELECTED_PROVIDER`, and `..._PROVIDERS`. If a child tries to run `llm-scheduler --suspend-until-ready` while Ralph is active, the scheduler exits with status `75` instead of suspending — Ralph remains the single rotation and suspend coordinator.
