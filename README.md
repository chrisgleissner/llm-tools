# llm-usage

Tiny Linux CLI for checking remaining Codex, Claude Code, and GitHub Copilot usage from one terminal command.

`llm-usage` is a small Bash utility for developers who use multiple AI coding tools and want a quick local usage snapshot without opening several apps or dashboards.

```log
Last refreshed: 2026-06-02 18:30:00
Tool                       Window         Remaining    Remaining Time   Resets             Time to Reset
------------------------   ------------   ----------   --------------   ----------------   ------------
Codex                      5h             78%          -                2026-06-02 18:49   19m         
Codex                      weekly         53%          -                2026-06-07 16:25   4d 21h 55m  
Claude                     5h             69%          22m              2026-06-02 23:20   4h 49m      
Claude                     weekly         62%          -                2026-06-04 13:00   1d 18h 29m  
Copilot                    monthly        79%          -                2026-07-01 00:00   28d 5h 29m  
```

### Output columns

`Tool`: Provider or row type (`Codex`, `Claude`, `Copilot`, plus `ai-credits` when enabled).  
`Window`: Quota window (for example `5h`, `weekly`, `monthly`).  
`Remaining`: Remaining quota as a percentage (or `-` when unavailable).  
`Remaining Time`: Estimated time to consume the current remaining quota at the recent burn rate; `-` when unknown/unreliable.  
`Resets`: Local reset time for that window, formatted as `YYYY-MM-DD HH:MM` in your local time zone.  
`Time to Reset`: Relative time until the window reset.  
`Source` (shown with `--show-source`): Origin of the row data (`codex`, `claude cache`, etc.).

### Cell coloring

The `Remaining` column is color-coded when color output is enabled:

* Red: remaining usage is under 10%.
* Yellow: remaining usage is 10% to 29%.
* Green: remaining usage is 30% or above.
* Unknown/unavailable values (for example `-`) are not colorized.

Colors are disabled automatically when output is not a TTY, when `TERM=dumb`, or when `LLM_USAGE_NO_COLOR` is set.

## Features

* Shows remaining usage for Codex, Claude Code, and GitHub Copilot.
* Reads local usage state where possible.
* Can query Claude usage from the Anthropic API when suitable credentials are available.
* Captures Copilot usage from the local Copilot CLI footer.
* Prints a compact terminal table by default.
* Supports JSON output for scripts and status integrations.
* Supports watch mode with in-place refresh.
* Uses terminal colours to make low remaining usage easy to spot.
* Runs as a single executable script with no build step.

## Install

`llm-usage` is intended to live in `~/.local/bin`.

### Clone and install

```bash
git clone https://github.com/chrisgleissner/llm-usage.git
cd llm-usage
install -m 0755 llm-usage ~/.local/bin/llm-usage
```

### Install directly with curl

```bash
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/chrisgleissner/llm-usage/main/llm-usage \
  -o ~/.local/bin/llm-usage
chmod +x ~/.local/bin/llm-usage
```

Make sure `~/.local/bin` is on your `PATH`.

```bash
command -v llm-usage
llm-usage
```

## Usage

```bash
llm-usage
llm-usage --json
llm-usage --watch 5
llm-usage -w 5
llm-usage --show-copilot-credits
llm-usage --statusline
llm-usage --no-header
```

## Options

| Option                   | Description                                                             |
| ------------------------ | ----------------------------------------------------------------------- |
| `--json`                 | Print JSON instead of a table.                                          |
| `--watch SECONDS`        | Refresh every `SECONDS`, replacing the previous table in-place.         |
| `-w SECONDS`             | Short form of `--watch`.                                                |
| `--show-copilot-credits` | Include the Copilot AI credits row in table and JSON output.            |
| `--statusline`           | Read Claude Code statusline JSON from stdin and cache it for later use. |
| `--no-header`            | Omit table headers.                                                     |
| `-h`, `--help`           | Show help.                                                              |

In watch mode, the table is redrawn in-place and includes a refresh timestamp:

```log
Last refreshed: 2026-06-02 13:52:18
```

## Data sources

`llm-usage` combines several provider-specific sources:

| Tool           | Source                                                                                    |
| -------------- | ----------------------------------------------------------------------------------------- |
| Codex          | Local session JSONL files under `~/.codex/sessions`.                                      |
| Claude Code    | Local cache, local Claude project/session files, and optionally Anthropic usage API data. |
| GitHub Copilot | Usage text shown by the local Copilot CLI footer.                                         |

Claude cache files are stored under `~/.cache/llm-usage`.

## Dependencies

Required:

* Bash
* `jq`
* `curl`
* GNU coreutils, including `find`, `sort`, `tail`, `date`, and `timeout`
* `python3` or `python`

Optional:

* Copilot CLI, available as `copilot` or `github-copilot`, for live Copilot usage capture
* Local Codex state under `~/.codex/sessions`
* Local Claude credentials and session files under `~/.claude`

## Scope and limitations

`llm-usage` is a practical local helper. It is not an official provider tool and it is not a billing dashboard.

It does not:

* manage subscriptions
* change provider limits
* guarantee authoritative billing or entitlement data
* upload usage data to third-party services
* aim to support every LLM provider
* currently support non-Linux systems

Provider formats can change. If Codex, Claude Code, Anthropic, GitHub Copilot, or their CLIs change local files, API responses, authentication layout, or terminal output, some fields may become unavailable or stale.

## Privacy

`llm-usage` reads local Codex and Claude state from your home directory. It may use Claude credentials to query Anthropic usage, and it may invoke the Copilot CLI locally to capture the visible usage footer.

It does not upload data anywhere other than direct provider API calls needed to fetch usage.

## Tests

```bash
./llm-usage-tests.sh
```

The tests are fixture-driven and cover table output, JSON output, missing Copilot data, timeout handling, and isolation between Copilot, Codex, and Claude parsing.

## License

Apache License 2.0.
