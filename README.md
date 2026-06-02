# llm-usage

[![Build](https://github.com/chrisgleissner/llm-usage/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/chrisgleissner/llm-usage/actions/workflows/test.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)](https://github.com/chrisgleissner/llm-usage/releases)

`llm-usage` is a small Bash CLI that shows a compact snapshot of remaining quotas for:

- Codex
- Claude Code
- GitHub Copilot

Run `llm-usage -w 60` to see your live token use, updated every 60 seconds:

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

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/chrisgleissner/llm-usage/main/llm-usage -o ~/.local/bin/llm-usage
chmod +x ~/.local/bin/llm-usage
command -v llm-usage
```

## Usage

```bash
llm-usage
llm-usage --json
llm-usage --watch 60     # refresh every 60 seconds
llm-usage --show-copilot-credits
llm-usage --show-source
llm-usage --statusline
```

### Command options

- `--json` print JSON output
- `--watch/-w <seconds>` refresh continuously
- `--show-source` show where each row was derived from
- `--show-copilot-credits` include Copilot AI credits row
- `--statusline` read Claude statusline JSON from stdin and cache it
- `--no-header` hide the table header
- `-h, --help` show usage

## Output columns

- `Tool`: `Codex`, `Claude`, `Copilot` (`ai-credits` if enabled)
- `Window`: `5h`, `weekly`, `monthly`
- `Remaining`: percentage or `-`
- `Remaining Time`: estimated burn time to zero usage, or `-`
- `Resets`: local reset timestamp (`YYYY-MM-DD HH:MM`)
- `Time to Reset`: duration until reset

`Remaining` is colorized by default:

- green `>= 30%`
- yellow `10–29%`
- red `< 10%`
- unknown values remain uncolored

## Data sources

- Codex: `~/.codex/sessions`
- Claude: local cache and optional Anthropic usage API
- Copilot: local Copilot CLI footer parser

## Requirements

- Bash, `jq`, `curl`, GNU coreutils, `python3` (or `python`)
- Optional: `copilot` command for live Copilot usage

## Notes

- The tool is local-first and not an official billing dashboard.
- Missing rows or `-` values mean data was unavailable or not parseable.
- Provider formats can change; output may become stale if local/remote formats change.

## Tests

Run: `./llm-usage-tests.sh`

## License

Apache License 2.0.
