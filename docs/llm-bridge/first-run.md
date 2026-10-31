# First run: from install to the first completed prompt

This page records a walk-through of the Bridge first-run path as a new user would take it, the snags found, and what was changed. The README section [Your first Bridge mission](../../README.md#your-first-bridge-mission) and `llm-bridge help quickstart` describe only steps that were run here.

## Setup of the walk-through (2026-10-30)

* Install: `pipx install <checkout>` into a fresh `PIPX_HOME` and `PIPX_BIN_DIR`. The documented `pipx install git+https://github.com/chrisgleissner/llm-tools.git` was also run into a second fresh pipx home and installed llm-tools 1.2.1.
* No llm-tools configuration: `LLM_TOOLS_CONFIG=/nonexistent`.
* State directory: `XDG_STATE_HOME` pointed at a directory that did not exist, so Bridge had to create `llm-tools/llm-bridge` itself. The leaf directory was created with mode `700` and `bridge.sqlite3` with mode `600`. The parent directories that Bridge creates take the user's umask (`775` here).
* The real `HOME`, so Claude Code stayed signed in. Codex and OpenCode were installed and signed in as well.
* A new empty git repository, a scratch tmux session of 120 by 34 cells, driven with `send-keys` and `capture-pane`.
* One real prompt, Claude Code with `ANTHROPIC_MODEL=claude-sonnet-5-5` set in the environment, because the home form has no model setting. The Claude session log shows four assistant records from `claude-sonnet-5-5` and one from `claude-opus-5-5`; the Opus record was not caused by the task and its source was not investigated.

## Timed path

| Step | Time | What appeared |
| --- | --- | --- |
| `pipx install` (checkout and git URL) | 12.7 s and 12.3 s | `installed package llm-tools 1.2.1`, five commands listed |
| `llm-usage` | 12.1 s to the table; the spinner `refreshing usage 6/7` is visible from the start | A table with seven providers, two of them `unavailable`. A second run took 12.3 s. |
| `llm-bridge doctor` | 0.1 s | State directory, database `integrity ok`, tmux, three providers `installed, logged in`. Before the changes below there was no next step. |
| `llm-bridge` (home) | 0.26 s | `Choose a mission` and `New mission in this folder`, footer `N new mission` |
| `N` (form) | 0.22 s | Objective, Providers, Folder, Mode, Save, Start |
| Typing the objective and moving to Start | operator pace | Before the changes below, Providers showed only `[1] codex`. |
| Enter on Start | immediate | Claude Code's folder trust prompt, with `No, exit` preselected |
| Answer `Yes, I trust this folder` | 7 s after Start (operator pace) | Claude wrote `hello.txt` ("Brewed for 4s") and asked to be exited with `/exit` |
| `/exit` | 1 s | `Provider exited (status 0) · Task 1 is waiting for your review. Ctrl-] then B, then /approve or /reject.` |
| `Ctrl-]` `B`, `/approve`, `y` | 15 s at operator pace | Controls with `task 1 awaits approval · type /approve 1`, a confirmation dialog, then `✓ COMPLETED`, `1/1 tasks completed` |
| `Ctrl-]` `d`, `Ctrl-D` | immediate | `Detached from mission … the mission is completed` |

From Start to COMPLETED took 31 s, about half of it operator pauses. `hello.txt` contained `hello`.

The automated version of this path (`tests/test_bridge_first_run_path.py`, fake `claude` that writes the session record and `hello.txt`) takes about 4.5 s, of which about 1 s is the provider run.

## Snags and how each was handled

| # | Snag | Handling |
| --- | --- | --- |
| 1 | The home form selected `codex` alone whatever was installed or signed in. Reaching Claude took deselecting codex, moving the cursor and toggling claude (6 key presses), and the list reorders selected entries to the front while the cursor stays put. | `default_providers` now selects the first installed and signed-in provider in the order claude, codex, opencode, copilot, kilo, and keeps `codex` only when nothing is signed in, so validation can explain what is missing. A configured rotation still wins. The reordering was not changed. |
| 2 | `doctor` listed providers but gave no next step. | `doctor` ends with a `Next` line: install tmux (or use `--headless`), sign in with the named command, or the first-mission steps. The same list is in the JSON as `next_steps`. |
| 3 | Claude Code's folder trust prompt appeared with `No, exit` preselected. Bridge had said nothing about it. Choosing it makes Claude exit with status 1 (checked outside Bridge and inside a mission, without sending a model prompt). Bridge showed `Provider exited (status 1)`, kept the task RUNNING and started Claude again about 70 s later, when the prompt came back. The cause was the scheduler's retry delay (60 s) after a non-zero exit. | The Start hint adds `Claude asks once to trust this folder: choose Yes`, only when Claude is the first provider, the mode is Native and `~/.claude.json` (or `$CLAUDE_CONFIG_DIR/.claude.json`) does not record the folder or a parent as trusted. `doctor` has the same note, and the README and quickstart say what to choose. The README also says what happens on `No`. Bridge does not edit Claude's trust file. Later change: when the provider is Claude Code, exits with a non-zero status within 15 minutes, and the end of its output is the trust prompt with `No, exit`, the scheduler returns the distinct status 76 instead of retrying. Bridge records the operation as exited 76, holds the task BLOCKED and records a `provider-declined` event. Status and the Needs you row show `Claude Code was not allowed to trust this folder · trust it and run /start again, or choose another provider with /rotation`. Nothing is started again. `/start` (or `llm-bridge start`) releases the held task, which was observed to leave the dialog on screen with the process exited (`EXIT=1`) in a tmux pane. |
| 4 | Nothing said that Tab moves down to the Start row. | The Objective hint now reads `Type the task · Tab moves on to Start · Ctrl-E full editor · paste supported`. |
| 5 | The quickstart told the user to run `task verify --evidence '…'` to approve. Without `--approve` the command only records evidence and the task keeps waiting. | The quickstart uses `task verify --task 1 --approve --evidence '…'`. |
| 6 | `llm-bridge --version` was a usage error, so a new user could not confirm the install. | `llm-bridge --version` prints the installed version. `llm-usage`, `llm-scheduler` and `ralph-robin` still reject `--version`. |
| 7 | `llm-bridge start` without a terminal printed how to attach, but not how the task ends. | The line now says that `llm-bridge status` shows how to approve once the provider has finished. |
| 8 | README claims that were not observed: the capacity sample `5h 66% left, lasts past reset`, and a `Day one` block that mixed steps not run together. | The sample is replaced by the line the Overview showed, and the first-mission section lists only steps that were run. |
| 9 | With a provider that records no session file, Bridge took about 12 s after the provider exited to move the task out of RUNNING, with no feedback (seen with a fake provider; real Claude Code was recognised within a second). | Not changed. The automated test's fake provider writes the session record like Claude does. |
| 10 | `llm-usage` takes about 12 s on each run on this machine and shows a spinner. | Fixed. With empty caches the first table now appears after 3.2 to 3.8 s and the complete table after 5.5 to 8.5 s, against 17.6 to 18.9 s before at the same load. Causes: the Copilot footer capture always waited out its 10 s timeout before asking the quota API; `kilo stats` took 6 s although the exact figure comes from Kilo's database; and an ephemeral sampler waited 5 s before the direct read started. Details and measurements are in `docs/llm-usage.md`. |
| 11 | The home subtitle lists every installed provider, including ones that are not signed in. | Not changed. |

## Automated coverage

* `tests/test_bridge_first_run_path.py` drives the real home screen in a private tmux server with a fresh state directory, no configuration and a fake `claude`: `doctor`, `N`, objective, default provider, Start hint, Start, the provider exit hint, `/approve`, confirmation, COMPLETED and the stored mission state. It also covers the `Next` steps for a machine without tmux, without a signed-in provider and without any provider.
* `tests/test_bridge_first_run_helpers.py` covers the trust lookup (missing file, unreadable file, parent folder, `CLAUDE_CONFIG_DIR`), the default provider choice, the Start hint and `--version`.
* Reverting `default_providers` to select `codex` makes the first test fail at the `[1] claude` assertion.
