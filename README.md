# llm-usage

Tiny Linux CLI for showing remaining Codex, Claude Code, and GitHub Copilot usage.

Small Linux Bash utility that reads local usage state where possible and prints a compact
usage snapshot for Codex, Claude Code, and GitHub Copilot in one terminal table.

## Example output

```
Tool       Window        Remaining       Used Resets           Source
---------- ------------ ---------- ---------- ---------------- ----------------
Codex      5h                  47%      53.0% 2026-06-02 13:49 ~/.codex/sessions
Codex      weekly              59%      41.0% 2026-06-07 16:25 ~/.codex/sessions
Claude     5h                   0%     100.0% 2026-06-02 13:20 api.anthropic.com/api/oauth/usage
Claude     weekly              75%      25.0% 2026-06-04 13:00 api.anthropic.com/api/oauth/usage
Copilot    monthly             80%        20% -                copilot cli
Copilot    ai-credits      unknown          0 -                copilot cli
```

## What it does

- Shows remaining usage for Codex, Claude Code, and GitHub Copilot.
- Focuses on a quick local status check for developers using multiple AI coding tools.
- Reads available local state and optionally queries the Anthropic usage endpoint when credentials are present.
- Outputs a human-friendly table by default and optional JSON output for tooling.
- `--watch` now prefixes each refresh with `Last refreshed: YYYY-MM-DD HH:MM:SS` and appends new rows without clearing the screen.
- Percentages use terminal color for quick scanning (red → yellow → green by remaining usage).
- Runs as a tiny executable script with no build step.

## What it does not do

- It is not a billing dashboard.
- It is not an official provider tool.
- It does not manage subscriptions.
- It does not change provider limits.
- It does not send usage data to a third-party service.
- It does not aim to support every LLM provider.
- Linux-only for now.

## Installation

The command is a single file intended for `~/.local/bin/llm-usage`.

```bash
cp llm-usage ~/.local/bin/llm-usage
chmod +x ~/.local/bin/llm-usage
```

Recommended clone-based install:

```bash
git clone https://github.com/chrisgleissner/llm-usage.git
cd llm-usage
install -m 0755 llm-usage ~/.local/bin/llm-usage
```

One-command install path (after publishing):

```bash
mkdir -p ~/.local/bin && curl -fsSL https://raw.githubusercontent.com/chrisgleissner/llm-usage/main/llm-usage -o ~/.local/bin/llm-usage && chmod +x ~/.local/bin/llm-usage
```

Make sure `~/.local/bin` is on your `PATH`.

Verify:

```bash
command -v llm-usage
llm-usage
```

## Usage

```bash
llm-usage
llm-usage --json
llm-usage --watch 5
llm-usage --statusline
llm-usage --no-header
```

Options:

- `--json`: print JSON.
- `--watch SECONDS`: refresh every `SECONDS`.
- `--statusline`: read Claude Code statusline JSON from stdin and cache it for `--statusline` mode.
- `--no-header`: omit table headers.
- `-h`, `--help`: show help.

## Dependencies

- Bash
- jq
- curl
- GNU coreutils (`find`, `sort`, `tail`, `date`)
- `timeout` (usually in coreutils)
- python3 or python
- Copilot CLI (`copilot` or `github-copilot`) if you want live Copilot capture
- Optional local state:
  - `~/.codex/sessions`
  - `~/.claude` credentials / session files

## Data sources and caveats

- Codex is read from local `.codex` session JSONL files under `~/.codex/sessions`.
- Claude usage is read from local cache (`~/.cache/llm-usage/claude-status.json`) and local
  project/session files under `~/.claude/projects`, with live API usage from `api.anthropic.com/api/oauth/usage` when credentials are available.
- Copilot rows come from the Copilot CLI’s screen footer as observed by a local pseudo-tty capture.

If any provider changes local file formats, API responses, authentication layout, or terminal output,
any field may become unavailable or stale. This script is a practical local helper, not an authoritative
billing or entitlement system.

## Privacy

llm-usage reads local Codex and Claude state under your home directory, may use Claude credentials to query
Claude usage, and may invoke the GitHub Copilot CLI locally to capture its visible usage footer. It does not
upload data anywhere other than provider API calls needed to fetch usage.

## Tests

Run:

```bash
./llm-usage-tests.sh
```

The tests are fixture-driven and validate table output, JSON output, missing Copilot data handling,
timeout behavior, and Copilot isolation from Codex/Claude JSON structure.

## License

Apache License 2.0.

## Status

Small, intentionally lightweight CLI with one executable and one test script.
