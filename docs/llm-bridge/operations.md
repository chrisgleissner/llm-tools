# LLM Bridge operations

LLM Bridge stores missions and task history locally. It connects task order,
provider operations and verification evidence, so an operator can inspect what
happened before approving completion or retrying uncertain work.

This guide is for the person who runs missions. It is organised by what you do,
from installing Bridge to deleting an old mission. Reference material (tmux
station details, event classes, the retention disk budget, appearance and
environment variables) is collected in the sections at the end.

Related guides:

- [User guide](guide.md): concepts.
- [Troubleshooting](troubleshooting.md): error codes and symptoms.
- [Uninstall](uninstall.md): removing llm-tools.
- [Distributed operations](../distributed/operations.md): workers and coordinators.

## Contents

| If you want to... | Go to |
| --- | --- |
| Install Bridge and start a first mission | [Install and create a mission](#install-and-create-a-mission) |
| Script Bridge, read exit codes, report a bug | [Command-line use](#command-line-use) |
| Work in the console and its views | [Daily console use](#daily-console-use) |
| Add, verify, approve or reject tasks | [Tasks and verification](#task-ordering-and-verification) |
| Pause, stop, resume or cancel work | [Attach, pause, stop, and resume](#attach-pause-stop-and-resume) |
| Work over SSH or inside your own tmux | [Remote operation (SSH)](#remote-operation-ssh) |
| Find state, set retention, back up, upgrade, delete | [Local state and config](#local-state-and-config) |
| Sleep until provider credits recover | [Capacity and suspend](#capacity-and-suspend) |
| Know what a provider can and cannot do | [Provider trust boundary](#provider-trust-boundary) |
| Run the chaotic-input tests | [Testing with chaotic input](#testing-with-chaotic-input-fuzzing) |
| Fix a problem | [Troubleshooting](#troubleshooting) |
| Look up station keys, event classes, disk budget, colours, variables | [Reference](#reference) |

## Install and create a mission

### Requirements

- Install `llm-tools` as usual. The existing provider CLIs and credentials are
  used by the scheduler. Bridge adds no provider SDK or network service.
- Native and Background missions use a tmux station, so they need `tmux`.
- Headless (`--mode headless`) runs a mission without tmux.
- The installed executable works outside the checkout. After upgrading an older
  local editable installation that lacks the new commands, refresh it with
  `pipx install --force --editable /path/to/llm-tools`.
- `command -v llm-bridge` should resolve the pipx launcher.

### Check the installation

```bash
llm-bridge doctor
```

`doctor` checks private storage integrity and tmux availability. For each
provider in the rotation (the mission's with `--mission`, otherwise the default)
it checks whether the command is on `PATH` and whether the sign-in files its CLI
writes exist, for example `codex: installed, logged in` or `claude: not on PATH`.

- It ends with a `Next` line: install tmux, sign in, or the first-mission steps,
  including Claude Code's one-time folder trust prompt.
- It reads local files only and sends no request. An expired token is found
  when the provider runs.
- Running `llm-bridge` with no arguments and without a terminal prints help and
  does not create mission state.

### Use the home screen

Run `llm-bridge` with no arguments from any working directory.

The home screen lists saved missions with status, short id (the id the CLI
accepts as `--mission`), name, age and folder. With no missions it explains what
a mission is.

| Marker or key | Meaning |
| --- | --- |
| `●` | The mission's folder is the current folder. |
| `✕ INTERRUPTED` | Replaces the status of a RUNNING or PAUSED mission whose controller stopped unexpectedly (crash, kill or reboot). The detail line gives the fix, `llm-bridge start --mission ID`. |
| Up / Down, Enter | Choose a mission. |
| N | New mission. |
| ? | Help. |
| Q | Quit. |
| S | Start the selected created or stopped mission. |
| o | Open the selected mission's controls. |

On a mission with a live native station, Enter attaches to the provider
directly (detaching returns to the list). Any other mission opens its controls.
After a native start returns from the station (detached or provider exited),
Bridge opens the mission's controls. Ctrl-D leaves controls and Q leaves the
home screen.

### Create a mission in the home form

Press **N**. The form defaults to the current folder and Native interactive.

1. With no configured rotation, the form selects the first provider that is
   installed and signed in (Claude Code, then Codex, OpenCode, Copilot, Kilo).
   A machine with only one signed-in CLI starts with that one.
2. The checklist offers only provider CLIs found on `PATH`; missing ones are
   labelled "not installed". After them come routes from the llm-tools config
   whose launch CLI is installed, each with its pinned model.
3. Selected entries come first in rotation order (`[1]` is on top). The others
   follow alphabetically. Space toggles an entry.
4. Tab or Enter moves between fields, and each focused field shows a hint line.
   Space changes Native/Headless (Headless starts with `--headless`).
5. Ctrl-E opens the multiline objective composer. Escape keeps a private
   recovery draft.
6. **Save mission only** saves the first task without starting provider work.
   After a save, the home screen stays open with the new mission selected and
   says `Saved f292f491 · S start · o controls`. The controls show `saved as
   f292f491` once.
7. **Start and open provider** is the last row. It says `Starts claude now ·
   uses provider capacity`, naming the first selected provider. It saves the
   mission, starts its controller, then attaches to its validated station.
8. Repeated Enter walks the fields and stops on Save, so Start needs Tab/Down and
   then Enter.
9. The Headless choice runs the provider without a terminal and with no station.

For a Claude Code selection that has never been started in the folder, the Start
hint adds `Claude asks once to trust this folder: choose Yes`.

The home screen enables bracketed paste, so newlines inside a paste stay in the
objective. For terminals without bracketed paste, an Enter that arrives inside a
burst of input is treated as pasted text. It never starts or saves a mission, and
on the Start or Save button it only shows a hint to press Enter again.

CLI mission creation is save-only. For a multiline objective use the composer or
`mission create --file`.

### Create a mission from the command line

Create a mission from a file (or use `--prompt TEXT` for a short objective):

```bash
llm-bridge mission create --file objective.md --cwd "$PWD" --json
llm-bridge start --mission MISSION_ID
```

The result includes the mission ID and the initial task ID. `mission create`
only creates the mission; start it separately.

- `mission create` warns on stderr when the folder already has an unfinished
  mission (`warning: this folder already has unfinished mission 14f56611 "..."
  (CREATED); llm-bridge task add --mission 14f56611 ... adds to it`). It still
  creates the new one. `--new` suppresses the warning.
- The list printed when folder inference is ambiguous shows each mission's
  last-updated time. The `Needs you` line and receipts spell out `--mission ID`
  so the command works as copied.

**Prompt size limit.** A provider receives the objective and task prompt as one
command-line argument, and Linux accepts at most 131,071 bytes in one argument.
A larger prompt is refused before the provider starts: the task is marked
`FAILED`, a `task-launch-refused` event records `prompt too large for the
provider command line (N bytes)`, and no other provider is tried. Shorten the
task, or put the detail in a file in the working folder and refer to it from the
prompt. A mission command template that reads `{prompt_file}` is not subject to
the limit.

### Choose a mode

A mission runs in one of three modes. The same three names appear in the home
form, `--help`, `llm-bridge help` and `status`.

| Mode | `--mode` | Provider | Station |
| --- | --- | --- | --- |
| Native | `native` | the provider's own interactive terminal UI | tmux station with the provider in its pane; open it with `attach` |
| Headless | `headless` | runs without a terminal; Bridge shows its activity output | none; no tmux needed, nothing to attach to |
| Background | `background` (CLI default) | runs without a terminal, controlled by a background controller | tmux station whose provider pane tails the controller log; the Bridge pane takes control commands |

- The home form offers all three (Space or Left/Right switches) and starts on
  Native, which is recommended for interactive work.
- The command line defaults to Background because it needs no terminal, so a
  script behaves the same on every machine.
- Older documentation and saved events call Background mode "autonomous".
  Earlier controllers stored that old name as the `actual_mode` of
  `controller-mode` events. Bridge shows such events as `background` and leaves
  the stored row unchanged. Copies replicated to the coordinator keep the old
  stored name.
- Mission JSON carries `"mode": "native|background|headless"` in `config`, next
  to the saved `headless` and `native_tui` booleans.

Pass `--mode native|background|headless` to `mission create` or `start`. A mode
on `start` overrides and saves the mission's mode.

The older `--native` and `--headless` flags still work as aliases of `--mode
native` and `--mode headless`. A flag that contradicts `--mode`, or `--native` with `--headless`,
is refused (`--headless and --native cannot be combined`).

### Choose providers and routes

- The rotation comes from the llm-tools config (`[ralph]` before `[defaults]`),
  else the built-in `claude,codex,opencode`, limited to the providers the config
  enables. `mission create --help` prints the rotation a new mission gets now.
- `--routes` names configured route IDs and wins over `--providers`.
- An explicit `--providers` without `--routes` clears configured routes, so the
  providers you list are what runs.
- `llm-bridge rotation --set LIST` is the one documented way to change a
  rotation of a saved mission. `--routes` and `--providers` still work.

`mission create` and `start --providers/--routes` check every rotation entry
with the check `rotation --set` uses.

- A name that is neither a provider nor a configured route is refused with a
  suggestion before anything is saved: `unknown rotation entry 'claud' · did you
  mean claude?`.
- An entry whose CLI is installed but not signed in only produces a warning on
  stderr: `warning: codex is not signed in (run: codex login); it will be skipped
  until it is`. The check reads the same files as `doctor`, offline.
  `LLM_BRIDGE_NO_SIGNIN_CHECK=1` turns the warning off.

### Start a mission

- `start` refuses a cancelled or completed mission with a message and exit
  status 4 (wrong state).
- It prints `running` only after the controller has reported ready.
- A controller that fails first gives `mission ID could not start: REASON · fix
  it with: llm-bridge rotation --mission ID --set LIST`.

### Mission options

These are mission creation options, not entries in the shared TOML file.

| Option | Default | Allowed range |
| --- | --- | --- |
| `--verifier 'COMMAND'` (split like a shell, not run through one), or the command last after `--` | none | |
| `--verifier-arg` (repeat once per argv element) | none | |
| `--verifier-timeout` | 300 seconds | 1–3600 seconds |
| `--log-max-bytes` | 4 MiB | 1 KiB–1 GiB |
| `--log-rotations` | 3 | 0–20 |
| `--log-retained-runs` | 20 | 1–1000 |
| `--capacity-refresh-seconds` | 60 | 5–3600 seconds |

Appearance settings live in `[bridge.theme]` (`name`, `background`, `roles`); see
the [colour guide](colour-guide.md). The `[bridge]` and `[bridge.retention]` keys are
listed under [Local state and config](#local-state-and-config).

### Resolving `--mission`

- `--mission` (short `-m`) accepts a full mission ID or a unique prefix of 3 or
  more characters.
- When it is omitted, Bridge uses the one unfinished mission whose folder is the
  current folder or a parent of it. A live mission wins over stopped or failed
  ones.
- With no match, or several, the error lists candidates.
- CLI commands print human-readable text. Add `--json` (short `-j`) for the
  stable machine-readable form.

## Command-line use

### Command-line conventions

- **One list command.** `llm-bridge mission list` and `llm-bridge missions` print
  the same table: `ID`, `STATE`, `NEEDS YOU` (`review task 1` when a task awaits
  your review), `TASKS` (done/total), `NAME`, `FOLDER` and `UPDATED`, live
  missions first. The current provider and task are in `missions --json`
  (`provider`, `current_task`). `console` is an internal command and is not
  listed.
- **Task numbers.** A task is named by the number `task list` shows:
  `llm-bridge task approve 1` is the same as `llm-bridge task approve --task 1`
  (short `-t`). `--task-id ID` takes the global ID, which is internal. JSON task
  objects carry both `number` (an alias of `ordinal`, the number you type) and
  `id` (the global ID). `proposal accept N` and `proposal reject N` take the task
  number in the same way, and `llm-bridge view notes.md` reads a file without
  `--file`.
- **Options are spelled out.** A long option is never accepted as a prefix:
  `stop --f` is an error, not `stop --force`. Explicit aliases (`--prompt-file`,
  `-f`, `-m`, `-t`, `-j`) work.
- **Pagination.** Every list command takes `--limit N` and ends a partial list
  with `showing N of M <things> · --limit N shows more · <continuation>`.
- **Coordinator URLs and `--target`.** `worker enroll --coordinator` and `remote
  login --coordinator` accept either `https://HOST:PORT` or `wss://HOST:PORT` for
  the same coordinator and store the scheme each command needs. `--target NAME`
  goes before the command (`llm-bridge --target build-1 mission list`). After the
  command it is an error that shows the corrected command line.
- **Mistyped commands.** A mistyped command or option gets one line and exit
  status 2 (an error object on standard output with `--json`), for example
  `llm-bridge: unknown command 'stauts' · did you mean status? · llm-bridge
  --help lists commands`. Internal commands (`console`, `_controller`,
  `_station-gc`) are not listed or suggested.

Pagination by list command:

| Command | Behaviour |
| --- | --- |
| `events` | Newest N; `--after EVENT_ID` reads forward. |
| `task list` | First N; `--offset N` continues; `--all` lists every task. |
| `proposal list` | `--after TASK_NUMBER` continues. |
| `mission list` / `missions` | Live first; 100 in text, all with `--json`. |

`--json` lists are objects, never bare arrays: `{"tasks": [...], "total": N,
"offset": N}`, `{"missions": [...], "total": N}`, `{"events": [...]}`,
`{"proposals": [...]}`, `{"workers": [...]}`.

### Exit status and error output

| Status | Meaning |
| --- | --- |
| 0 | Success, or nothing to do (`Already paused`, `Already stopped`, `Nothing to pause`). |
| 1 | Runtime failure: a provider, the controller, the disk or the network failed. `doctor` exits 1 when no provider can run. |
| 2 | Usage error: unknown command or option, a value that is not valid, a command refused for a managed provider. |
| 3 | Not found or ambiguous: no such mission, task, ID prefix or folder mission, or a prefix that matches several; also `task output`/`task diff` when there is nothing to show. With `--target`, 3 also means the target did not answer (the request stays queued). |
| 4 | Wrong state: the mission or task is not in a state that allows the command (`start` on a cancelled mission, `task approve` on a task that is not in review, `finish` without a native provider, `attach` without a station, `mission delete` on a running mission). |

With `--json`, an error is printed on standard output (not standard error), with
the same exit status:

```json
{"error": {"code": "mission-not-found", "help": "https://github.com/chrisgleissner/llm-tools/blob/main/docs/llm-bridge/troubleshooting.md#mission-not-found", "hint": "run llm-bridge mission list", "message": "no mission matches 'zzzz'"}}
```

- `hint` is the command that fixes the problem, or an empty string.
- `help` is the address of the entry for the code in the
  [troubleshooting guide](troubleshooting.md). The entry heading is the code.
- In text mode an error exit (status 1 to 4) ends with the same address on its
  own line, `see: URL#CODE`.

| Code | Exit | Meaning |
| --- | --- | --- |
| `usage` | 2 | |
| `invalid-argument` | 2 | |
| `mission-not-found` | 3 | |
| `task-not-found` | 3 | |
| `not-found` | 3 | |
| `mission-ambiguous` | 3 | |
| `wrong-state` | 4 | |
| `conflict` | 4 | A task changed since you read it. |
| `schema-too-new` | 4 | The database was written by a newer llm-bridge; see "Database upgrades, backups and downgrades". |
| `refused-in-managed-session` | 2 | |
| `failed` | 1 | |

The same table is in `llm-bridge --help` under "Exit status". Every code and the
common symptoms are explained in [troubleshooting](troubleshooting.md).
`tests/test_docs_coverage.py` fails when a code produced by the source has no
entry there.

The [user guide](guide.md) covers the concepts and the five tasks every operator
does, including `llm-bridge task amend`, `llm-bridge proposal
list|accept|reject|create`, `llm-bridge pause`, `llm-bridge finish`, `llm-bridge
usage`, `llm-bridge backup`, `llm-bridge restore` and the remote and worker
commands.

### Inspect a mission

```bash
llm-bridge status --mission MISSION_ID
llm-bridge events --mission MISSION_ID --limit 20
llm-bridge events --mission MISSION_ID --since 10m --follow
llm-bridge doctor --mission MISSION_ID
```

`status` reports the mission, tasks and their states, recent operations, pending
commands and recent events. `--json` selects machine-readable output. It does not
create, start or otherwise change a mission.

`events` shows the newest events (the last 100 by default), oldest first, in the
same words as the console's Activity view.

| Option | Effect |
| --- | --- |
| `--limit N` | Show the newest N events. |
| `--since 10m` | Keep only recent events (`90s`, `2h`, `1d`). |
| `--follow` | Print new events until Ctrl-C. |
| `--after ID` | Read oldest first from an event ID. |
| `--verbose` | Include controller bookkeeping (heartbeats, mode, prompt and wake records). |
| `--json` | Keep the raw fields (`kind`, `data`) and add the `total` of matching events. |

- Event IDs are global across all missions, so a mission's first event is not ID 1.
- A `command-rejected` event carries the reason the command was refused.

`doctor` reports the state directory, SQLite integrity and tmux availability,
then checks every provider the home form offers (or the mission's own rotation
with `--mission`).

- It exits 0 when at least one provider is installed and signed in, and 1
  otherwise.
- `--json` adds `ready` (boolean) and `blocking` (list of reasons).

How `status` reports mode and model:

- The monitor reports configured mode separately from actual controller mode.
  Actual mode is shown only after a verified controller-mode event.
- Provider liveness and observed model remain `unknown` when no provider
  observation proves them.
- An operation's model is a configured scheduler pin, not an observed model.

### Diagnostic bundle for bug reports

```bash
llm-bridge doctor --bundle                 # llm-bridge-bundle-<UTC time>.tar.gz in this folder
llm-bridge doctor --bundle report.tar.gz   # or a path (a folder gets the default name inside it)
```

`doctor --bundle` writes one archive with mode 0600 and prints its path and a
one-line note on what was removed. It never overwrites a file. It exits 0 when
the archive was written, whether or not a provider can run (that answer is in
`doctor.json`).

| Member | Content |
| --- | --- |
| `versions.json` | llm-tools, Python and operating system versions |
| `doctor.json` | the `doctor` report; the mission appears only as its ID and status |
| `config/llm-tools.toml` | the llm-tools configuration file |
| `config/worker.json`, `config/remote.json` | worker enrolment and coordinator metadata; the token is replaced by `token_present` |
| `store-summary.json` | schema version, mission, task and event counts, statuses, and per-mission counts; no names or text |
| `health.json` | controller liveness per mission |
| `logs/...` | the last 200 lines (at most 64 KiB) of the controller log and of recent provider logs; prompt copies are never included |

Every text member passes through one redaction function
(`llm_tools/bridge_bundle.py`, `redact`). It removes:

- bearer and API tokens (`sk-`, `ghp_`, `github_pat_`, JWT-shaped values, long
  hex and base64 strings, private keys);
- values of keys named like token, secret, password, api_key or authorization;
- e-mail addresses;
- private and LAN addresses (10/8, 172.16/12, 192.168/16, 100.64/10, 127/8,
  fc00::/7, fe80::/10, ::1);
- host names under `.home.arpa`, `.local`, `.lan` and `.internal`;
- the home folder, which is replaced with `~`.

Mission names, objectives and task text are removed from log tails by exact
match, line by line and word by word for long words. A provider that echoes a
prompt reworded or wrapped differently can still leave a fragment, so read the
archive before attaching it to a public issue.

The coordinator has its own `bridge-coordinator support-bundle`. This command
does not collect coordinator data.

### Other command-line facts

- `llm-bridge screen --mission ID` prints the last 200 lines of a native
  provider's current terminal screen (each line cut at 1000 characters; `--json`
  adds a `truncated` flag). It reads the station and changes nothing.
- `llm-bridge control` needs a terminal on both stdin and stdout. See
  [Open the control screen](#open-the-control-screen).
- `llm-bridge usage` prints the `llm-usage` table once, so a script can read it.
- See `llm-bridge --help` and the command-specific `--help` output for all
  available CLI operations.
- Read a local textual artifact with `llm-bridge view notes.md` (`--file
  notes.md` also works). See [Document viewer](#document-viewer).

## Daily console use

Bridge has two console forms. The compact console runs in the tmux station and
the full control screen opens from the command line. Both accept the same
command handlers, and control commands use the same handlers as the CLI.

### Compact console

The compact console is available in the tmux station. It has a command entry
line and refreshes mission status in the remaining six lines, which include the
current mission and task summary. Type a command and press Enter. `help` lists
the console commands, including task and mission controls.

The `›` command line accepts slash commands as well as bare commands.

| Slash command | Effect |
| --- | --- |
| `/help [TOPIC]` | Help. |
| `/start` | Open the provider station in native mode. |
| `/attach` | Attach to the station (also works from inside your own tmux). |
| `/approve [N]` | Approve task N. N may be omitted when exactly one task is in `REVIEW`. |
| `/continue` | Start the next task while a reviewed one stays in `REVIEW`. |
| `/pause`, `/resume`, `/stop`, `/cancel` | Mission controls. |
| `/status`, `/tasks`, `/events` | Show status, tasks, events. |

Other console commands, where N is the per-mission task number. File paths with
spaces can be quoted.

| Group | Commands |
| --- | --- |
| Mission controls | `pause`, `resume`, `stop`, `cancel` |
| Task controls | `verify N`, `reject N REASON`, `priority N VALUE`, `reorder N VALUE`, `dependencies N PREREQUISITE_N`, `retry N`, `pause N`, `resume N`, `cancel N` |
| Task text | `add FILE`, `amend N FILE` |

- Typing `/` opens a list of the slash commands with a one-line description
  each. It narrows as you type.
- Tab completes a single match, or only the common prefix of several. A second
  Tab names the matches, so an ambiguous prefix is never completed to the first
  command.
- An unknown command is answered with close matches.
- `cancel`, `stop --force`, task cancellation and approval ask for a compact
  confirmation: **Y** confirms, **N** or **Esc** cancels.
- The Overview's `Needs you` row names tasks awaiting approval.

### Open the control screen

```bash
llm-bridge control --mission MISSION_ID
```

`control` needs a terminal on both stdin and stdout. Without one (a pipe, a
script, `ssh` without `-t`) it prints `llm-bridge: control needs an interactive
terminal; use llm-bridge status` and exits 2.

The control screen offers nine views on F1–F9.

| View | Shows |
| --- | --- |
| Overview | Mission state, current work, task counts, acceptance-criteria progress, a `Needs you` row, and a `Recent` section with the latest mission events (time, kind, summary). |
| Tasks | Tasks with a priority column (`P5`) and a live preview of the selected task: title, revision, acceptance criteria, evidence and the start of the prompt. |
| Providers | Providers and stations. |
| Activity | Heartbeats collapsed into readable summaries; recent bounded history. |
| Sessions | Recent bounded history of provider sessions. |
| Health | Configured mode and actual controller mode, kept separate. |
| Actions | Operator actions; filter with `/`. |
| Usage | The embedded `llm-usage` dashboard. |
| Agents | Roots across missions and exact parent-bound Codex children. |

- The names used before the rename (Bridge, Orders, Stations, Log, Systems,
  Palette) are still accepted wherever a view is named.
- A status-coloured header names the mission and its state. A footer lists the
  keys that apply to the current view; `?` opens help for the current view.
- The Health, Providers, Sessions, Actions and Usage views use aligned columns.
- While a task is running and the provider screen has been quiet for 30 seconds,
  its Now row and the station status line show `no output for 2m`.
- Capacity is phrased in plain language, for example `5h 66% left, lasts past
  reset, resets 2h 5m`. "Lasts past reset" means the current pace will not
  exhaust the window before it resets.

**Tasks view paging.** Tasks are fetched in pages of 200. Page Up and Page Down
navigate them, and Ctrl-F opens the Tasks filter. Search and page controls make
tasks past the first page reachable without loading the whole queue into the
terminal process. The preview follows the selection; `v` opens the complete
prompt and Enter inspects the task.

**Providers and Health.**

- Providers distinguishes the station selected for viewing from the last
  station selected to execute work.
- Selecting a station with Enter, or running
  `llm-bridge station --mission ID NAME`, changes only the view preference. It
  does not change routing, start a provider or switch an executing operation.
- Models are labelled as configured or observed, so a model pin is never
  mistaken for provider-reported identity.
- In Health, `unknown` means there is no verified observation.

### Keys and mouse

| Input | Action |
| --- | --- |
| Tab / Shift-Tab, Left / Right, F1–F9 | Change view while body focus is active |
| Up / Down, PgUp/PgDn, Home/End | Navigate the selected list or open document |
| Click a view label | Change view |
| Click an Actions row, then Enter | Select and run that action |
| Wheel up / down | Scroll the view and move the selection (three rows per notch) |
| Page Up / Page Down in Tasks | Previous / next 200-task page |
| Ctrl-F, type query, Enter | Filter Tasks by task title or prompt; Escape cancels and restores the command draft |
| Tasks: `v` | Open the selected task's complete prompt (the preview below the list shows its start) |
| Agents: `i`, Enter, Escape | Inspect the selected agent, switch to an existing root station where available, return to the map |
| Body Ctrl-E | Compose a new multiline task |
| Tasks body Ctrl-R | Compose an amendment of the selected task |
| Composer Ctrl-O | Open `$VISUAL` or `$EDITOR` |
| Composer Ctrl-S | Save source; Enter inserts a newline (F-keys only switch views, so F2 is not Save) |
| Composer Ctrl-X | Discard the draft; Escape or Ctrl-D closes it and keeps the draft |
| Overview body `o` | Open the complete objective |
| Actions `/` | Filter labelled actions; single click only selects |
| Enter | Run the command in the input line |
| Ctrl-P / Ctrl-N, or Up / Down in the focused input | Previous / next command history item |
| Ctrl-U | Clear the input line |
| Ctrl-K | Open Actions from any view, with the filter ready |
| Ctrl-G | Focus the current command draft |
| Escape | Leave command input and retain its draft |
| Ctrl-D / Ctrl-C | Exit when the input line is empty |

- The UI accepts printable command text without using letters as view shortcuts.
- Actions and keyboard commands use the same dispatcher.
- The command result pane summarizes durable command envelopes as a concise
  applied/rejected message. Detailed state remains available in the Activity
  view, CLI status and event history.
- The station follows terminal resize events and is designed to remain usable
  down to 80 columns by 24 rows.

### Composer

The composer keeps recoverable source in private state.

- Ctrl-O launches the external editor as argv on a private file. Returning from
  that editor does not submit the task.
- Ctrl-S explicitly saves the composed source.
- Escape or Ctrl-D closes the composer and keeps the draft (`Esc close (draft
  kept)`).
- Ctrl-E in the console reopens the newest kept task draft of the last 24 hours
  with `Restored draft from HH:MM · Ctrl-X discard`.
- Ctrl-X is the only key that discards.
- An empty task composer shows dim guidance on the first line (title,
  description, optional `Acceptance:` list).
- The footer shows a line count, and the byte count only above 80% of the 1 MiB
  limit.
- Rejected saves keep their source.

In the launcher, Ctrl-E opens the same full-screen composer for a long initial
objective. Enter inserts a newline and Ctrl-S saves to the form. The interactive
limit is 1 MiB of UTF-8 source bytes, and an oversized paste is rejected
atomically. Use `mission create --file PATH` or `--file -` for larger inputs.

### Document viewer

Read a local textual artifact with `llm-bridge view notes.md` (`--file notes.md`
also works).

| Key | Action |
| --- | --- |
| `/` | Search |
| `n` / `N` | Repeat the search |
| `m` | Toggle source / rendered text |
| `w` | Toggle wrapping |
| Left / Right | Scroll long source lines when wrapping is off |
| `s` | Export the complete original source to a new path (never a rendered or abbreviated preview) |

- Use `--follow` explicitly for live output: Up pauses, End resumes. New output
  and file rotation or truncation are labelled.
- Retention remains bounded, and pruned content is unavailable.
- Without a TTY, `view` prints original source.
- Home `?` opens the same searchable help viewer and returns to the launcher on
  close.
- A rendered document uses Rich when available. Source viewing and CLI help
  still work if Rich is unavailable.

### Help

Built-in help works offline.

- `llm-bridge help` lists every topic with a one-line description: quickstart,
  terms, keys, console, prompts, markdown, providers, states, verification,
  recovery, remote and distributed. The list lives in
  `bridge_help.TOPIC_SUMMARIES` and feeds every other list of topics.
- `llm-bridge --help` groups the commands (Start here, Missions, Tasks, Run
  control, Station, Capacity and power, Data, Distributed, Appearance) and lists
  the global options `--version` and `--target WORKER`.
- Each command page has one example. Commands that change something also have an
  `Effects:` line, for example `start` uses paid capacity and
  `suspend-until-credits` suspends this computer.
- `/help` in the console shows the slash commands generated from the same
  groups, with the shell command each shortcut stands for.
- Help pages (`?`, `/help`) open as a word-wrapped page with `↑↓ scroll · / find
  · Esc close` and none of the file viewer's controls.
- `?` on the home screen shows a short home-screen key summary, and `T` there
  opens the quickstart.
- `llm-bridge theme` ends with how to set the theme (`--palette-size` and
  `--colour` replace the older `--colors` and `--color`).

### Steering

Typed steering is available in either UI and on the CLI.

| Kind | Effect |
| --- | --- |
| `replace TASK FILE` | Replace a task prompt at the next safe boundary |
| `review TASK [FILE]` | Queue a review task, optionally from a prompt file |
| `verify TASK` | Request the configured verifier for a task awaiting verification |
| `pause-after-iteration` | Pause after current iteration, including while waiting for provider capacity |
| `stop-after-task TASK` | Stop after the selected task reaches completion |
| `handoff ROUTE` | Prefer the named configured route at the next selection |

CLI form: `llm-bridge steer --mission ID --kind KIND`, with:

- `--task N` (the per-mission task number; `--task-id ID` takes the global ID);
- `--to PROVIDER` (older name `--route`);
- `--file PATH` or `--prompt TEXT`, as required by that kind.

`llm-bridge steer --help` lists only `handoff` and `review`. The other kinds still
work for older scripts, but have their own commands:

| Steer kind | Own command |
| --- | --- |
| `replace` | `task amend` |
| `verify` | `task verify` |
| `pause-after-iteration` | `pause --after-task`: the mission pauses when the provider's current invocation ends, also during a wait for capacity |
| `stop-after-task` | `stop --after-task [N]`: without N it waits for the task that is running now; it cannot be combined with `--force` |

The steer spellings remain hidden aliases. See `llm-bridge steer --help` and
`steer help` in the console for the live syntax.

### Agents view and `agents` command

- `llm-bridge agents --mission ID` shows one mission. `--all-missions` shows the
  combined CLI map.
- Managed providers can inspect only their own mission.
- Root age measures the current Bridge operation. Child age uses the provider's
  session creation timestamp. Known completion freezes elapsed age. Missing
  creation or completion metadata stays unknown. Age ticks locally without a
  model call.

The inspector distinguishes observed model and effort from configured values and
unknown fields.

- Codex's read-only persisted-thread API can report `notLoaded` even while a
  worker is doing work in another runtime. Bridge keeps that native status
  separate from observed task lifecycle.
- Bounded, exact-session activity is labelled as last observed, with its
  timestamp and partial or stale freshness.
- Bridge reads tool names and lifecycle tags, never tool arguments, results or
  hidden reasoning.
- Other providers expose Bridge roots only; their native child enumeration is
  currently unavailable.
- Enter switches only to an existing live Bridge station where supported;
  otherwise it opens the inspector. No navigation action resumes or forks an
  agent.
- `LLM_BRIDGE_CODEX_APP_SERVER_CMD` overrides the read-only observation command
  for local protocol fixtures. Ordinary use discovers the installed Codex CLI.

### Usage view

- The view retains the usage renderer's cell colours through Bridge's palette,
  including separate red, yellow, green and cyan guidance. Explicit `NO_COLOR`,
  `LLM_USAGE_NO_COLOR` and monochrome terminals still suppress colour.
- Text and column fitting use the same dashboard as `llm-usage`. The status line
  names the source (`live llm-usage read`).
- By default the view lists only the mission's rotation providers. `a` shows
  every enabled provider whose CLI is installed.
- Under a capacity injection (`LLM_SCHEDULER_USAGE_JSON`,
  `LLM_SCHEDULER_USAGE_JSON_FILE`) the view renders the same snapshots the
  controller reads.
- When a live read still differs from the controller's last observation by more
  than 5 percentage points for a window, a line under the status gives the
  controller's figures and their age.
- An exhausted weekly or monthly window reads `× empty`.

### Mission list, picker and colours

The home list:

- orders live missions first, then waiting ones (created, stopped, failed), then
  finished ones (dimmed);
- re-reads the database every two seconds;
- drops the folder, then the provider, first at narrow widths;
- filters with `/` (or Ctrl-F) by name, folder or ID prefix. Enter opens the
  match and Esc clears the filter.

A mission with a task awaiting your review (`REVIEW`) shows `◆ NEEDS YOU`
instead of `● RUNNING`, in the home list and in the picker. Its detail line says
`task N awaits your review · Enter to open`.

The mission picker (`llm-bridge missions --pick`, or Ctrl-] then M in a station):

- counts a mission as live only while it is not finished and has a running
  controller or station;
- shows `station idle` (muted) for a finished mission whose tmux station is still
  running;
- shows `INTERRUPTED` for a RUNNING, PAUSED or STOPPING mission without a running
  controller, as on the home screen.

With a `station idle` mission selected, **x** (or Ctrl-X) asks for confirmation
(**Y**/**N**) and then closes that station through the same safe path the
controller uses. A station whose provider process is alive, or to which a
terminal is attached, is kept. `llm-bridge gc [--json]` does the same for every
finished mission.

Choosing another mission in the picker moves your terminal to that station. The
station you left keeps running. One detach (Ctrl-] then d) from the new station
returns you to where you started the first one (your shell or the home screen).

Mission identity colours are described under [Appearance](#appearance).

## Task ordering and verification

### Task states

The task lifecycle uses `QUEUED`, `READY`, `RUNNING`, `VERIFYING`, `COMPLETED`,
`FAILED`, `BLOCKED`, `PAUSED` and `CANCELLED`.

- Only `COMPLETED` tasks satisfy dependencies.
- Provider exit code zero moves work to `VERIFYING`. Completion requires explicit
  operator approval or a passing configured verifier.
- `VERIFYING` is the stored value (database, JSON `status`, remote protocol).
  Every screen and table shows the same state as `REVIEW`, because the task is
  waiting for you. Task JSON also carries `"status_label": "REVIEW"`.
- A mission with a task in `REVIEW` shows `NEEDS YOU` instead of `RUNNING` in the
  console header, the station strip, the home list and the picker.

### Task numbers

- Tasks are numbered within their mission: the first task of every mission is
  `task 1`. Numbers are assigned at creation and never reused.
- `--task N`, `--depends-on`, `--add` and the console aliases (`/approve 2`,
  `/reject 2 REASON`) all take that number, and receipts and errors use it.
- JSON output shows it as `ordinal` next to the global `id`. `--task-id ID`
  selects a task by the global ID.
- A number outside the mission is reported with the valid range, for example
  `mission f292f491 has tasks 1–3; there is no task 5`.
- Databases created before schema 7 are numbered on first open, by creation
  order within each mission.

### Add and order tasks

Add a task with a verbatim file or standard input. Dependencies refer to task
numbers and prevent a task from becoming ready until each prerequisite is
completed. Higher priority runs first, then lower explicit order, then older
task.

```bash
llm-bridge task add --mission MISSION_ID --file follow-up.md \
  --priority 5 --depends-on 1 --acceptance "tests pass"
llm-bridge task list --mission MISSION_ID        # first 200 tasks in run order; --all, or --limit N --offset N
llm-bridge task show --mission MISSION_ID --task N
llm-bridge task output --mission MISSION_ID --task N   # provider log of the last run; --follow while it runs
llm-bridge task diff --mission MISSION_ID --task N     # working-tree change since that run started
llm-bridge task priority --mission MISSION_ID --task N --value 10
llm-bridge task reorder --mission MISSION_ID --task N --value 20
llm-bridge task dependencies --mission MISSION_ID --task N --add PREREQUISITE_N
llm-bridge task retry --mission MISSION_ID --task N
llm-bridge task pause --mission MISSION_ID --task N
llm-bridge task resume --mission MISSION_ID --task N
llm-bridge task cancel --mission MISSION_ID --task N
```

`task list` shows the tasks in the order the controller runs them, with finished
tasks last.

| Column | Meaning |
| --- | --- |
| `RUNS` | The place of a task that still waits to run (1 runs next). |
| `PRIORITY` | A higher number runs first. The default is 0, negative numbers run last, and there are no P0 to P3 urgency levels. |
| `ORDER` | Among equal priorities, a lower order number runs first. |

`task priority` and `task reorder` say where the task now runs, for example
`Priority of task 3 set to -5 · runs after tasks 1 and 2`.

### Amend a task

```bash
llm-bridge task amend --mission ID --task N --file revision.md
```

Amendments preserve the earlier prompt and increment the task revision.

- When no provider step is working on the task, the new text applies at once
  (`Amended · task 2 is now revision 3`).
- While a provider is running the task, the amendment waits for the end of that
  step. `task show` then lists it as `pending amendment (applies after the
  current step)` next to the text still in use.
- Prior evidence may no longer apply, so an amended task that was awaiting
  approval is queued again and must be verified against its new revision.
- Because that changes what would be approved, `task amend` refuses a task in
  `REVIEW` unless you pass `--requeue`. The earlier result is then sent back and
  the amended task runs again (uses capacity).
- A task title that came from the first prompt line follows the new prompt.

### Reopen a completed mission

`task add` on a completed mission reopens it, with or without `--mission` (the
folder's one completed mission is used when no unfinished one exists).

- The mission becomes `CREATED` (not started yet) and a `mission-reopened` event
  is recorded.
- The receipt says `Mission ID reopened · start it with: llm-bridge start
  --mission ID`.
- A cancelled mission takes no tasks.

### Review: approve or reject

A provider's successful exit moves its task to `REVIEW` (stored as `VERIFYING`).
Success alone does not complete the task.

`task verify --evidence TEXT` alone only records the note, which `task show` lists
with its source and age. The task stays in `REVIEW`. Approve it explicitly, with
optional evidence:

```bash
llm-bridge task approve --mission MISSION_ID --task N \
  --evidence "Reviewed implementation and test output"
```

To send a task back instead, give a reason.

```bash
llm-bridge task reject --mission MISSION_ID --task N --reason "tests were not run"
llm-bridge task reject --mission MISSION_ID --task N --reason "tests were not run" --hold
```

- The task is queued for another attempt, and its next prompt quotes the reason.
- The retry starts at once when a controller is running and uses paid capacity.
  The receipt says so.
- `--hold` keeps the task paused instead. Resume it with `task resume`.
- In the console, `/reject N REASON` asks for confirmation with two choices: `Y`
  rejects and retries, `H` rejects and holds (the same as `--hold`).

### Verifier

For deterministic checks, configure a verifier at mission creation.

```bash
llm-bridge mission create --file objective.md --cwd "$PWD" \
  --verifier './verify-task {context}' --verifier-timeout 120 --json
```

- Write the command as `--verifier 'python -m pytest -q'`, or after `--` at the
  end of the command line (`-- python -m pytest -q`).
- Bridge splits it into words as a shell would, does not invoke a shell, and
  prints the result as an argument list in the creation receipt.
- For exact quoting, `--verifier-arg` is one argv item; repeat the flag to form
  the command. Give only one of the three forms.
- `--verifier-timeout` defaults to 300 seconds and must be greater than zero and
  no more than 3600 seconds.
- If no verifier is configured, the task remains in `REVIEW` for operator
  review. A failing verifier or timeout also leaves it uncompleted.

Placeholders:

| Placeholder | Value |
| --- | --- |
| `{context}` | A private JSON file with the objective, task prompt, revision and acceptance criteria |
| `{cwd}` | The mission working directory |
| `{mission_id}` | The mission ID |
| `{task_id}` | The task ID |
| `{revision}` | The task revision |

Verifier result:

- The verifier succeeds when it exits zero.
- JSON output is optional. When present, `summary` and an `artifacts` list are
  recorded, and `passed: false` vetoes success.
- Artifact paths are checked relative to the mission working directory.

**Verifier tamper check.** The provider works in the same folder as the verifier
and can edit it. When the mission is created, Bridge records a SHA-256 of:

- the verifier program and of the script it interprets (for example `check.py`
  in `python check.py`), when they are inside `--cwd`;
- any file named with `--verifier-file PATH` (repeatable, relative to `--cwd`).

If one of them has changed, been deleted or (for a symlink) been repointed by
the time a task is verified, Bridge does not run the verifier and does not
complete the task.

- The task stays in `REVIEW` with a `verification-failed` event whose reason is
  `verifier-modified` and which lists the changed paths. You approve it
  explicitly.
- Programs outside `--cwd` (such as `/usr/bin/python3`) are not tracked.
- Missions created before this check have no recorded hashes and behave as
  before.

See `llm-bridge task verify --help` and `llm-bridge mission create --help` for
the full flag list.

### Native missions hold for review

A native mission is interactive, so its controller does not start the next task
while a finished task awaits your decision. After a provider run ends with task 1
in `REVIEW`, the controller waits (it records a `review-hold` event) until you
do one of these:

- approve task 1 (`/approve 1`);
- reject it (`/reject 1 REASON`, which queues it again);
- release the hold with `/continue`.

Only then does the next task start. Background and headless missions are
unattended and keep starting the next task straight away.

- `continue` (`/continue` in the console, `llm-bridge continue [--mission ID]`)
  releases the hold once. The next ready task starts while the reviewed task stays
  in `REVIEW`. If that task later returns for review, or the reviewed task is
  rejected and comes back, the mission holds again. With no hold it reports
  `Nothing to continue` and changes nothing.
- `--auto-continue` on `mission create`, or `llm-bridge mission set
  --auto-continue on|off` for an existing mission, turns the hold off (or on).
  Turning it on while the controller is holding releases the hold at once.
- With a configured verifier, a passing verifier completes the task and nothing
  holds. A failing verifier leaves the task in review and holds.
- There is nothing to hold back when no other task can start (no further task,
  or the next one depends on the reviewed task). Then the task simply shows the
  usual `Needs you` approval.
- Approve, reject, continue and auto-continue each end the hold with a
  `review-hold-released` event. `how` is `approved`, `rejected`, `continue` or
  `auto-continue`, and `summary` is a human sentence.

While holding:

- `status` prints `Needs you   task 1 awaits your review · /approve 1 or /reject 1
  reason · /continue to start the next task now` and `Holding     the next task has
  not started for 3m · task 1 awaits your review`.
- The snapshot carries `hold` (`{"reason": "review", "task_ordinal": 1, "tasks":
  [1], "since": ISO time}`, or `null`) and the same text in `needs_you` (`kind`
  `approval`, `hold: true`).
- In the station, the provider pane's footer after a clean provider exit reads
  `Provider exited (status 0) · Task 1 ready for review · Ctrl-] B then /approve`
  (`/reject` is listed in the controls).
- tmux formats that footer when the process exits, so the controller sets it
  before launching a native run. It is omitted when a verifier is configured or
  auto-continue is on.
- A provider pane that is still an empty placeholder shows the same sentence as
  its placeholder text.

### Task notices to a running native provider

While a native Claude Code, Codex or Copilot works, tasks added or amended
meanwhile are announced to it with one line typed into its prompt (see the
guide).

- The line is typed only when the prompt is empty. The pane is read with its
  escape sequences, and text after the prompt glyph counts as empty only when it
  is absent or drawn faint (SGR 2), bright black (90) or in a 256-colour or
  truecolour grey, which is how the providers draw their placeholder text.
- Any other text is treated as an unsent draft and is never submitted.
- A notice held back by a draft for ten minutes records one
  `task-notice-deferred` event.
- A provider that does not draw its placeholder in one of those styles never gets
  a notice. The queue still works.

Turn the feature off with:

```toml
[bridge]
task_notices = false # default true
```

### Provider native teams and token spend preference

New missions default to provider-native team guidance enabled and Medium token
spend preference. Set global defaults in the shared `config.toml`:

```toml
[bridge]
teams = true
token_spend = "medium" # low | medium | high
```

- Override either choice for one mission with `mission create --teams on|off` and
  `--token-spend low|medium|high`.
- Change a saved mission with, for example, `llm-bridge mission set --mission ID
  --teams off --token-spend low`.
- `llm-bridge mission settings --mission ID` prints the saved settings (automatic
  continue, capacity-wait suspend, teams and token spend) without changing them.
- `llm-bridge status --json` reports `mission.execution_policy`. Readable status
  and the station Overview show the effective settings.
- Explicit model, effort, authorization and user prompt choices keep their
  existing precedence.

What the preferences mean:

| Setting | Effect |
| --- | --- |
| Teams on | Asks for native subagents only when the provider supports them and the work benefits from separation. Unsupported providers continue as one agent. |
| Teams off | Every token spend preference omits Bridge-added delegation guidance. |
| Low | Minimizes extra passes and repeated context, escalating when that cannot meet requirements. |
| Medium | Adjusts reasoning and review to task risk and uncertainty. |
| High | Permits more capable reasoning and independent skeptical checks when worthwhile. |

These preferences never weaken mandatory validation or acceptance.

- Existing missions with no saved policy retain legacy prompts until an operator
  sets either value. Setting teams on for such a mission uses Medium unless
  `--token-spend` is also supplied.
- Repeated task invocations receive the same short policy prefix, so it adds
  prompt overhead each time.
- User-supplied task and mission text is stored unchanged and appears after the
  prefix.

Measured overhead:

- The team-enabled policy is 104–106 whitespace-delimited words (750–773 UTF-8
  bytes, depending on preference). These are measured text sizes, not tokenizer
  counts.
- Each launch records `prompt-assembled` with incoming, assembled and
  Bridge-added UTF-8 byte counts, plus total wrapper bytes relative to distinct
  source content when assembled by the controller.
- Token count remains unknown without verified provider usage.
- Identical task and mission objective content appears once in the assembled
  context. Distinct task content and stored original text are preserved.
- No equivalent direct-provider run has established marginal token overhead or
  parity.

## Attach, pause, stop, and resume

### Attach and detach

```bash
llm-bridge attach --mission ID
```

Attach to the private tmux station with `attach`. Leave your own terminal with
Ctrl-] then d.

- Detaching only closes the terminal view. It does not pause the mission or stop
  a provider, and it leaves the controller and panes alive.
- After you detach with Ctrl-] then d, `llm-bridge attach` prints `Detached from
  mission f292f491 (name) · it keeps running · llm-bridge attach to return`
  instead of tmux's `[detached (from session mission-…)]` line. A finished
  mission says `the mission is completed` and so on.
- `attach` runs tmux with its stdout captured, because tmux prints that line to
  stdout and draws the screen through the terminal device. Any other tmux exit
  line, such as `[server exited]`, is passed through unchanged.
- Headless missions have no station to attach to. When no station is running,
  `attach` says so and names `llm-bridge start --mission ID`.

`llm-bridge detach --mission ID` is for another shell. It detaches the station's
single connected writable client, which is usually a different terminal from the
one running the command.

| Situation | Result |
| --- | --- |
| No client attached | It says so. |
| Several clients attached | It refuses and lists each terminal with its size and idle time (read-only observers are marked). Choose one with `--client /dev/pts/N`. |
| One writable client | It prints `Detached /dev/pts/18 from mission f292f491`. |

Read-only observers are never detached. Station keys and tmux settings are in
[Station reference](#station-reference).

### Pause and resume

`pause` prevents a new provider iteration from starting.

- If a provider is already working, it runs until that invocation returns, then
  the controller waits for `resume`.
- `resume` continues an active controller or starts one if needed.
- `pause` on a mission that is not running says `Nothing to pause · mission is
  not running (CREATED)`. On a paused one it says `Already paused · mission ID is
  PAUSED`. These no-ops exit 0.
- `pause` does not sleep the machine.

### Stop

`stop` never cancels a mission.

| Mission state | `stop` result |
| --- | --- |
| No controller (`CREATED`, `STOPPED` or `PAUSED`) | Changes nothing and says `Nothing to stop · mission ID is saved (CREATED) · start it with: llm-bridge start` (`Already stopped · ...` when it is `STOPPED`). Exit 0. |
| `RUNNING` with a dead controller | Moved straight to `STOPPED`, because nobody is left to finish a `STOPPING`. If a provider run was still recorded as active, the receipt names `stop --force` to end it. |
| `RUNNING` with a controller | Asks the controller to stop at its next boundary and closes the owned station after the controller stops. In native mode it waits for the provider invocation to return and says how to finish if that takes a while. A later resume creates or reconnects the station as needed. |

`stop --force` reconciles unresolved work and terminates only a process group or
tmux station whose persisted ownership can be validated.

- When no provider process was running it prints `Stopped · no provider process
  was running` and exits 0.
- It fails only when a recorded provider process cannot be confirmed as the
  mission's, and then names its PID.
- The task that `stop --force` interrupted is recorded as `interrupted by stop
  --force`, and the next `start` runs it again.
- Recovery at `start` and a repeated `stop --force` check the recorded provider
  identity again until termination has succeeded.
- A recorded process whose PID now has a different start token is treated as gone
  and is never signalled.
- When a `RUNNING` operation has no recorded process identity, `start` refuses on
  every attempt until you acknowledge with `stop --force`.

### Cancel

```bash
llm-bridge cancel --mission ID --yes
```

`cancel` durably cancels the mission and its unfinished tasks, then ends the
provider process of an active run using the same identity check as `stop
--force`. If that process cannot be ended automatically, the receipt names `stop
--force`.

- It ends the mission for good: `resume` and `start` refuse a cancelled mission.
- It asks `This cannot be undone` on a terminal and needs `--yes` when there is
  no terminal to ask on.
- The console's own dialog offers `Cancel mission` and `Keep mission`.
- A repeated `pause` or `stop` on a mission that is already paused or stopped says
  `Already paused` or `Already stopped`.
- `llm-bridge help quickstart` has a table of all the ways to stop work.

### When nothing can run

A running controller with nothing it can run (every unfinished task is blocked,
failed or paused, or the mission has no tasks) records one
`mission-waiting-for-operator` event, shown in Activity as `Waiting for you`.

- It polls less often the longer nothing changes (0.5 s up to 4 s).
- `status` and the Overview's `Needs you` row say `waiting for you` and name the
  command to run (`task retry`, `task resume`, `task approve`).
- The controller continues by itself once you act.
- A task that was cancelled counts as finished. A mission whose other tasks are
  all completed completes, and a mission whose tasks are all cancelled stops.

While a provider's capacity cannot be determined (not signed in, no usage data),
the task stays READY and no operation is open.

- `status` shows `Waiting for capacity data · claude has no usage data, launches
  anyway in 14m (at 19:50)`.
- The deadline is fixed when the wait starts (`--max-unavailable-wait`, default
  900 s), so the countdown only goes down.
- After it passes, Bridge launches the provider anyway.

### Waiting for capacity

When every rotation entry is out of capacity, the snapshot has a `waiting` object
(`reason`, `providers`, `until_epoch`, `detail`, `entries`).

- `status` prints `Waiting     for capacity · copilot resets in 2d 9h · change
  providers with: llm-bridge rotation --set LIST`.
- In native mode an empty provider pane shows `Waiting for capacity: …`. The
  scheduler replaces that pane when a provider launches. A pane that still holds a
  previous provider's output is not overwritten.
- By default, Ralph's managed readiness wait keeps the machine awake and remains
  interruptible. Pause, stop, cancel and boundary steering interrupt it promptly.
- To opt in to RTC-backed sleep during an all-rate-limited wait, run
  `llm-bridge mission set --mission ID --suspend-when-rate-limited on`. See
  [Capacity and suspend](#capacity-and-suspend).
- Bridge's background capacity-observation worker emits a fresh read-only
  capacity snapshot every 60 seconds by default (`--capacity-refresh-seconds`).
  Those provider reads do not run on the terminal input path.
- A user or external power manager can still put the machine to sleep
  independently.

### A native provider that runs out of quota

A native Claude Code or Codex session that runs out of quota only prints a notice
and waits for input. Examples: `Usage limit reached · limit resets 11:40pm` or
`You've hit your usage limit ... try again in 2 hours 13 minutes`.

While a native provider runs, the controller reads its pane every couple of
seconds. A line that begins with the provider's limit phrase on two consecutive
reads triggers these steps. A sentence that only mentions a usage limit does not
count.

1. Bridge records a `provider-usage-limit` event.
2. It stops the provider.
3. It marks that provider rate-limited until the reset time (local time, the next
   day when the time has passed). With no reset time it uses the local-block
   backoff, 300 seconds by default.
4. It continues the task with the next ready provider in the rotation.

With no other ready provider the mission waits, and `status` shows `Waiting for
capacity` with the reset time.

### Disabled providers

`[usage].providers` in the llm-tools config (or `LLM_USAGE_PROVIDERS`) lists the
providers every tool may use.

- `mission create`, `start`, `resume` and `rotation --set` refuse a rotation whose
  launch CLI is not listed, for example `copilot is disabled by [usage].providers
  in ~/.config/llm-tools/config.toml · add it there or set LLM_USAGE_PROVIDERS`.
- An existing mission is checked against the config frozen into it.
- A rotation typed by the operator is refused when any entry is disabled.
- A default or saved rotation is refused only when no entry is left. The
  controller drops disabled entries and continues with the rest, as ralph-robin
  does.

### Controller failure and finished stations

- When a controller fails, a `controller-failed` event records a one-line reason.
  `status` prints `Controller  failed — <reason>`, and the snapshot carries it as
  `lifecycle.failure_reason`.
- For missions without a live controller, `status` prints `stopped — run:
  llm-bridge start`, `paused — run: llm-bridge resume` or `finished` instead of
  the last heartbeat.
- A COMPLETED, CANCELLED or FAILED mission closes its tmux station when no client
  is attached. If a client is attached, the station closes when the last client
  detaches (the tmux `client-detached` hook runs the hidden `llm-bridge
  _station-gc --mission ID`).
- A native provider process that is still running keeps the station. `llm-bridge
  stop` still closes a retained station by hand.
- The snapshot's `operator_events` lists the latest 20 events that are not
  controller bookkeeping (heartbeats, capacity refreshes, process and checkpoint
  records). `events` is unchanged.

### Provider watchdog, questions and handoff

A provider run that Bridge's own watchdog stopped (status 75: no output for the
idle timeout, a question that stayed unanswered, or a blocked prompt) is handed
once to the next rotation entry. A second stop of the same task leaves it BLOCKED
and records `provider-handoff-exhausted`.

The question watchdog arms only while the last line of the provider's standard
output is a question.

- Later output, including a tool call, clears it. Standard error never arms it.
- A line is not a question when it is a warning (`<file>:<line>: SyntaxWarning:
  ...`, `DeprecationWarning`, `UserWarning`, `FutureWarning`, `RuntimeWarning`,
  `ResourceWarning`, Node `(node:123) Warning:`), sits in a Python traceback
  block, is quoted (`>`, quotation marks, backticks), is inside a code fence, or is
  indented as code.
- `common.pending_question` decides this for the headless and the PTY path alike.

When a run stopped on a real question, Bridge records `task-awaiting-answer` with
the question (up to 300 characters). While the task is BLOCKED, `status`, the
Overview `Needs you` row and the `needs_you` and `awaiting_answer` keys of the JSON
projection show the question and three ways forward:

1. Answer by putting the answer into the task text (`task amend --task N --file
   FILE`) and then `task retry`.
2. Continue unchanged (`task retry`).
3. Reject (`task cancel`).

The `mission-waiting-for-operator` event for that wait carries the question.

If the controller fails, it stops the provider process groups of unresolved
operations before the mission becomes FAILED. A group that cannot be stopped is
recorded as a `provider-orphaned` event with its pid. Two `start` commands for one
mission are serialized, and the controller records its pid and start token in its
lock file, which `stop --force` uses when `controller-ready.json` is missing.

### After a controller restart

After a controller restart, Bridge reconciles any operation that was in progress.
It may mark the operation uncertain and the task blocked for review.

- It does not treat a vanished process or a provider's zero exit as proof that
  the task is complete.
- It does not silently replay uncertain work.
- Inspect `status` and `events`, then use explicit verification, task retry or
  mission resume after resolving the uncertainty.

### Session continuation

The provider pane in native mode is a real provider terminal UI while that process
is running. Session continuation is provider-specific and is not inferred from
terminal text or timestamps.

**Claude Code and Copilot.** Bridge uses its own UUID only when the installed CLI
advertises `--session-id`.

- It records that session as `NATIVE_CREATED` only after structured provider
  metadata confirms the same UUID and working directory.
- On recovery it requests that exact ID only when the persisted Bridge-owned ID
  has the same provider and working directory and the CLI still advertises
  exact-ID resume.
- It reports `RESTORED_NATIVE` only after metadata confirms the resumed identity.
- Capability drift, missing metadata or a directory mismatch prevents exact
  restoration.

**Codex.** Bridge cannot choose a new Codex session ID.

- When the installed CLI supports the owned-process launch mode, Bridge inspects
  writable transcript descriptors in the validated provider process tree and
  accepts exactly one structured session identity with the expected directory. It
  never selects a transcript by recency.
- Unsupported capture, ambiguity or missing metadata leaves exact continuity
  unavailable.
- Old records without explicit Bridge ownership are ignored.
- Exact resume requires the saved ID, directory, provider metadata and current CLI
  capability to validate.

`RESTORED_NATIVE`, `NATIVE_CREATED`, `RECONSTRUCTED` and `UNAVAILABLE` are
distinct records. Do not treat a newly created native session as a restored
conversation.

Verification status:

- The Claude/Copilot recovery path has hermetic fake-provider coverage. Live
  native restoration has not been verified for those providers.
- The repaired bounded Codex probe verifies native reply, full controller stop,
  exact-ID restoration by a new controller, and context recall; see
  `final-native-mission.json` and the review ledger.
- When exact continuity is unavailable, the next prompt is reconstructed from
  durable mission objective, task revision, checkpoints, operation records and
  verification evidence.

## Remote operation (SSH)

Missions, the controller and the tmux station belong to your user account, not to
a login session. Log in over SSH (or open another terminal) to find a running
mission, inspect it and attach without disturbing the local session.

```bash
llm-bridge mission list            # "INTERRUPTED (was RUNNING)" marks a mission whose controller is gone
llm-bridge status --mission 3f9a   # unique ID prefix of 3+ characters
llm-bridge attach --mission 3f9a   # open the provider station
# Ctrl-] then d detaches your terminal; mission and local session continue
```

- `llm-bridge` and `llm-bridge control --mission ID` open the home screen and
  controls over SSH.
- Bridge looks for the station socket in `$XDG_RUNTIME_DIR/llm-bridge`,
  `/run/user/<uid>/llm-bridge` and `/tmp/lb-<uid>`. An SSH login without
  `XDG_RUNTIME_DIR` therefore still finds a station that was created from a
  desktop session.
- Detach with Ctrl-] then d, which removes only your own terminal.

**Several terminals.** Several terminals may be attached at once. tmux sizes the
window to the most recently active client (the station sets tmux `window-size
latest`), so the local view can be resized while a remote client of a different
size is typing. To avoid the resize, do one of these:

- detach the other client (Ctrl-] then d in that terminal, or `llm-bridge detach
  --mission ID` from a shell);
- use terminals of the same size.

`llm-bridge help remote` says the same.

**Clipboard.** Copies you make in tmux copy mode use OSC 52, so they reach your
local clipboard when your SSH terminal supports OSC 52. The provider cannot set
the clipboard itself. Terminals that block it fall back to Shift-drag selection or
tmux copy mode (Ctrl-] [).

**Distributed use.** `--target`, workers and coordinators are covered in
[distributed operations](../distributed/operations.md).

## Standalone use

`llm-bridge`, `llm-usage`, `llm-scheduler`, `ralph-robin` and `llm-sleep-soak` need no
coordinator, worker enrolment or remote configuration. Without an enrolled worker and
without `llm-bridge remote login`:

- No network connection is attempted and no host name is looked up. Bridge itself uses only
  a local SQLite database and a Unix-socket tmux server.
- No `worker/` directory or remote configuration file is created in the state directory.
  Only `llm-bridge worker enroll` and `llm-bridge remote login` create them.
- The worker, remote-client, WebSocket and protocol modules (`bridge_worker`, `bridge_remote`,
  `bridge_ws`, `bridge_protocol`) are imported lazily inside `llm-bridge worker`,
  `llm-bridge remote`, `llm-bridge worker list` and `llm-bridge --target NAME ...`. A
  local command does not import them, so it starts no slower than before.
- `llm-usage`, `llm-scheduler`, `ralph-robin` and `llm-sleep-soak` contain no reference to
  the coordinator at all.
- No configuration key is required, and exit codes and output of local commands are unchanged.

`tests/test_standalone_compatibility.py` runs a representative command of each tool with an
empty home, private state and runtime directories, and checks the points above by recording
non-Unix socket connections, name lookups and imports of the modules listed.

## Running Bridge inside your own tmux

You can run `llm-bridge`, `llm-bridge control` or the console command `/attach`
from a pane of your own tmux session.

- tmux refuses to attach from inside another tmux, so Bridge attaches to its
  private station with `TMUX` and `TMUX_PANE` removed for that call.
- The station appears nested in your pane. Ctrl-] is the station prefix, and your
  own tmux prefix is unchanged outside it.
- Ctrl-] then d detaches the station and returns you to your pane.
- Station key hints appear only inside a Bridge-owned station, not in other tmux
  sessions.

## Local state and config

### Where state lives

- Mission state defaults to `${XDG_STATE_HOME:-~/.local/state}/llm-tools/llm-bridge`.
  Set `LLM_BRIDGE_STATE_DIR` to use another directory.
- Bridge stores its SQLite database, private runtime files and logs there.
  Controller runtime files remain under the private Bridge state directory.
- tmux sockets use a short private runtime path under `${XDG_RUNTIME_DIR}/llm-bridge`
  when available, or `/tmp/lb-UID`, with a mission-ID hash in the socket name.
- Shared `config.toml` supports `[bridge]` team and execution defaults,
  `[bridge.theme]` appearance settings and `[bridge.retention]` data-retention
  settings. Choose providers, routes and execution mode on mission creation and
  start commands.
- The selected shared tool configuration is captured for the mission, so later
  changes do not silently alter its routing.

### Provider logs

- Provider output is rotated at 4 MiB per file with three rotations by default.
- Bridge retains the latest 20 run directories. When older directories are pruned
  it records log size and a SHA-256 digest in the mission database before deleting
  the files.
- Adjust these limits with the mission creation flags in
  [Mission options](#mission-options).

### Routine pruning

The controller records a heartbeat every few seconds and a capacity observation
every minute. To keep the database bounded, it prunes on every mission:

| Data | Rule |
| --- | --- |
| Heartbeats | Deleted when older than one hour. |
| Capacity observations, and `provider-wait` and `provider-selection` events (written on every pass of a capacity wait) | Thinned to one per 10 minutes when older than one hour. The newest event of each thinned kind is always kept. |
| Finished commands (applied, rejected or failed) | Deleted when older than 30 days. |
| Each pass that removed rows | Recorded as an `events-pruned` event. |

- Longer-term rules for finished missions are in
  [Data retention](#data-retention).
- While a mission is not finished, its audit records (task, operation outcome,
  verification, command and operator events) are never pruned. Only its
  diagnostic events older than `events_days` are.
- A `command-applied` or `command-rejected` event carries a short summary of a
  result larger than 4 KiB (for example the full prompt returned by `task.add`).
  The command record keeps the whole result.
- An observation identical to the previous one updates that row instead of adding
  another.
- The database file does not shrink after pruning. The freed space is reused. Run
  `VACUUM` while no mission is running to return it.

### Data retention

Bridge keeps four kinds of data on disk: the SQLite database (events, operations,
commands, tasks), provider run logs, verifier files and schema-migration backups.
Terminal scrollback is not one of them (see [Terminal scrollback](#terminal-scrollback)).

Settings live in `[bridge.retention]` of `~/.config/llm-tools/config.toml`:

| Key | Default | Meaning |
| --- | --- | --- |
| `events_days` | 90 | Diagnostic events older than this are deleted, in finished missions and in missions that are not finished. In a mission that is not finished the newest event of each kind stays and `capacity-refresh` events are thinned to one per hour instead of deleted. |
| `finished_missions_days` | 365 | A mission that finished longer ago than this keeps its mission and task rows and its audit summary events. Its other events, remaining provider run logs and verifier files are deleted. |
| `operations_days` | 180 | Operation rows of a finished mission are deleted this many days after they ended, once their logs are already pruned. |
| `migration_backups_keep` | 3 | Newest `bridge.sqlite3.v*.bak` files to keep; older ones are deleted. Allowed range 1 to 1000. |

- The three `*_days` keys accept 0 to 3650. 0 turns that rule off. Values must be
  whole numbers.
- A misspelt key or an out-of-range value stops the tool with exit status 2 and a
  message such as `bridge.retention: unknown key 'event_days' (...); did you mean
  'events_days'?`.
- A config file that cannot be read when the controller prunes leaves the defaults
  in force.
- The log settings chosen per mission, not in the config file, belong to the same
  budget: `--log-max-bytes` (4 MiB per output file), `--log-rotations` (3) and
  `--log-retained-runs` (the latest 20 run directories) on `mission create`.
- When older run directories are pruned, Bridge records each log's size and
  SHA-256 in an `operation-log-pruned` event before it deletes the files.

**What counts as finished.** A mission is finished when its status is COMPLETED or
CANCELLED and it has no operation that is still preparing or running.

- Only a finished mission is subject to `finished_missions_days`,
  `operations_days` and the deletion of audit records.
- A mission that is not finished (running, paused, stopped, failed or created) is
  subject only to `events_days`, and only for diagnostic events. Its audit
  records, audit summaries, tasks, operations and commands are never pruned,
  however old they are.
- The events of an operation that is still preparing or running are never deleted.
- The age of a finished mission is the time of its last update. Reopening a
  completed mission changes its status and restarts the clock.

**Enforcement.** The controller's background pruning pass (the same pass and
15-minute cadence as the heartbeat and capacity-refresh pruning) applies the
policy to every finished mission in the database. A running mission's controller
therefore also cleans up finished ones and other missions' diagnostic events.
Event classes and audit details are in [Retention reference](#retention-reference).

### Database upgrades, backups and downgrades

The database records its schema version in SQLite's `PRAGMA user_version`
(currently 11). Migrations are forward-only and run automatically the first time a
newer `llm-bridge` opens an older database, whichever command opens it. The
release history has used versions 2, 3, 6 and 10 (1.1.0 and 1.2.x shipped 6; 1.3.0
shipped 10).

Before a migration changes the version, Bridge copies the database (using
SQLite's online backup, so the copy is consistent) to the same private state
directory, mode 0600:

```
bridge.sqlite3.v<old schema>-<UTC timestamp>.bak     e.g. bridge.sqlite3.v6-20261106T094500Z.bak
```

- It also appends `<time> schema 6 -> 10; backup <path>` to `migrations.log` in
  that directory.
- A database that does not exist yet is created without a backup. Opening a
  database that is already current writes nothing.
- The migration runs in one transaction. If it fails, the database stays at the
  old version and the backup remains.
- Each schema change adds one backup. The background retention pass removes the
  oldest beyond the newest `migration_backups_keep` (3 by default; see
  [Data retention](#data-retention)) and appends a `pruned schema ... backup` line
  to `migrations.log` for each one it deletes.

**Downgrade.** An older `llm-bridge` refuses to open a database written by a newer
one. It exits with status 4 (JSON error code `schema-too-new`) and changes
nothing:

```
llm-bridge: this Bridge database is schema 12 but this llm-bridge understands up to 10;
upgrade llm-tools, or restore the backup /path/bridge.sqlite3.v10-20261106T094500Z.bak made
before the upgrade (copy it over /path/bridge.sqlite3)
```

The named backup is the newest one whose schema this `llm-bridge` understands.
Restoring it discards everything recorded after that upgrade.

1. Stop the controllers.
2. Remove the old `bridge.sqlite3-wal` and `bridge.sqlite3-shm` files.
3. Copy the backup over `bridge.sqlite3`.

If no backup is found, the message says so, and upgrading `llm-tools` is the only
way forward.

**Migration tests.** `tests/test_bridge_schema_migrations.py` opens a fixture
database of every earlier schema version (`tests/fixtures/bridge-schema/`, rebuilt
by `scripts/make_bridge_schema_fixtures.py`), checks that all rows survive and
that the migrated schema matches a freshly created one. Versions 7 to 9 and 1, 4
and 5 were never committed as source, so their fixtures are derived;
`MANIFEST.json` says how each was made.

**Backup and removal.** To back up the database, restore it on another host or
remove llm-tools completely, see [uninstall.md](uninstall.md). It lists every
file, unit and socket llm-tools creates and the command that removes each.

### Deleting a mission

```bash
llm-bridge mission delete ID
```

`llm-bridge mission delete ID` permanently removes a mission from this computer.
`ID` is the mission ID or a unique prefix of three or more characters. The current
folder is never used, so the mission must be named (`llm-bridge mission delete
--mission ID` is the same command).

**Which missions can be deleted.**

- Only a mission that never started (`CREATED`) or has ended or been stopped
  (`COMPLETED`, `FAILED`, `CANCELLED`, `STOPPED`) can be deleted.
- A `RUNNING`, `PAUSED` or `STOPPING` mission is refused with exit status 4 and a
  message that names the state and the command to use first (`llm-bridge stop
  --mission ID`, or `llm-bridge cancel --mission ID`).
- A mission whose controller is still exiting, or whose provider station is still
  open, is refused the same way. `llm-bridge gc` closes the idle stations of
  finished missions.
- Nothing changes when the command refuses.

**Confirmation.**

- The command asks `Delete mission 9a3c51e0 "name" with its tasks, events and logs?
  This cannot be undone.` and deletes only after you type `y`.
- `--yes` skips the question and is required when there is no terminal to ask on
  (without it the exit status is 2 and nothing is deleted).
- The refusal for a running mission comes before the question.

**What it removes.**

- The mission's rows: tasks, dependencies, amendments, operations, events, queued
  commands, managed processes and task counters.
- Its directory `runtime/mission-ID` in the state directory (the controller log,
  provider run logs and verifier files).
- The lock files, and the socket and configuration file of a station whose server
  is gone.
- It never touches the mission's working folder, and files of other missions stay.

**Repeated and ambiguous deletes.**

- Deleting twice reports `no mission matches ...` with exit status 3, as does an ID
  prefix that matches nothing.
- A prefix that matches several missions is also status 3 and lists them.

**Tombstones and coordinators.**

- A tombstone (mission ID, name, final state and time, without any content) stays
  in the database so the coordinator, when the worker is connected to one, can drop
  its copy even if the worker was offline when the mission was deleted. The worker
  sends it at the next connection.
- Tombstones older than 90 days, and all but the newest 1000, are removed.
- The `mission_tombstones` table is created by the first deletion. The schema
  version does not change.
- With a coordinator, `llm-bridge --target NAME mission delete ID` asks in the same
  way. A running mission is refused there too; with `--target` the worker's refusal
  is reported with exit status 2, like other refused remote commands.
- The web console shows **Delete mission** on the page of a deletable mission. It
  asks with the mission title, then returns to the missions list.
- The REST resource is `DELETE /api/v1/missions/ID?worker=NAME` with the body
  `{"confirm": true}`; see the [API reference](../distributed/api.md).

## Capacity and suspend

### Suspend until credits recover

```bash
llm-bridge suspend-until-credits --mission ID
```

Use `llm-bridge suspend-until-credits --mission ID`, or choose **Suspend until
credits recover** in Actions.

- Bridge refreshes the selected rotation's capacity and requires a genuine future
  reset.
- A balance with no scheduled reset, missing usage, or an already usable rotation
  cannot supply a recovery wake time.
- The command reports a refusal with its reason when it cannot establish a safe
  plan.

**Automatic capacity sleep** is opt-in with `mission set --mission ID
--suspend-when-rate-limited on`.

- Keep the mission controller running. Its background capacity worker also arms a
  wake while waiting for known credit recovery, even when the mission is paused or
  the UI is detached.
- This arming happens while the computer is awake, so a subsequent manual OS
  suspend already has a wake planned.
- A detached UI alone does not provide this service. The controller must remain
  alive.
- Arming a wake does not unpause a mission.
- Bridge sleeps only when no operation is active and its existing suspend path arms
  a wake. Pause, stop and cancel changes made during sleep take effect after wake.

### How wakes are armed and checked

- Wake requests are coordinated across missions. The earliest eligible request
  owns the next wake. Withdrawing one request preserves other owners.
- The shared power backend arms the wake before requesting sleep, and verifies
  timer availability.
- RTC readback is stronger evidence than a user timer. A systemd WakeSystem timer
  is checked for activity, and actual wake reliability is measured from the
  resumed wall clock.
- Firmware operations must hold a sleep block inhibitor and the device locks. A
  provider or uncertain active device operation prevents the explicit sleep
  action.
- External hardware tools must hold an OS sleep block inhibitor throughout a
  critical operation. Bridge checks these generic OS inhibitors alongside typed
  managed operation and process identity. Application device locks remain the
  hardware tool's responsibility.
- Host firmware, OS power support and a functioning wake backend remain
  prerequisites. Unknown recovery times keep the computer awake.

### What is recorded

Intent, target, arm result, sleep/wake outcome and drift are mission-correlated
events. Simulated outcomes are labelled as such; they do not establish hardware
wake reliability.

For a retrospective, correlate the mission and operation IDs with the suspend
cycle ID and wake unit.

- Controller startup records the source snapshot and process identity.
- Provider results record normalized failure causes and native versus scheduler
  exit status when the transport supplies them. Retained output is identified by
  its private path, byte count and digest.
- Capacity observations include their source, freshness and next eligibility.
- Power records distinguish awake waits, timer arming, accepted sleep requests,
  observed sleep and subsequent work.
- An accepted suspend command alone proves neither sleep nor resumed model work.
- Existing private log rotation and retention still apply.

### Capacity display and sharing

- Capacity lines use plain language, for example `5h 66% left, lasts past reset,
  resets 2h 5m`. "Lasts past reset" means the current pace will not exhaust the
  window before it resets.
- A background worker refreshes provider capacity every 60 seconds by default.
- Controllers share those reads through a private cache under
  `${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/shared-usage` (one file and lock per
  provider). With several missions running, a provider is queried about once a
  minute in total, not once per mission.
- The next task selection of a mission still reads the provider live.

### Safety summary

- `pause` does not sleep the machine, and Bridge adds no network listener.
- A zero provider exit leaves a task in `REVIEW` until an operator approves it
  (`/approve`, `task approve`) or a configured verifier passes.
- A bounded live probe verified Codex native reply, controller stop, exact-ID
  restoration and context recall. Claude and Copilot live restoration is
  unverified.

## Provider trust boundary

Providers run as your user, in your mission folder. Bridge narrows what a provider
can do by habit or by prompt injection; it does not sandbox it. The rules below
are guardrails and hygiene, not a security boundary. A real boundary needs a
separate OS user or a sandbox (container, `bwrap`, or a VM) for the provider
process.

| Rule | Summary |
| --- | --- |
| [Operator commands are refused](#operator-commands-are-refused-for-managed-providers) | A managed provider can run only read-only and proposal commands. |
| [Own mission only](#a-managed-provider-can-address-only-its-own-mission) | A managed provider cannot address another mission. |
| [Credentials](#providers-do-not-see-each-others-credentials) | Each provider loses other providers' credential variables. |
| [Prompts](#headless-codex-and-claude-prompts-are-not-in-the-process-arguments) | Headless Codex and Claude read the prompt from stdin. |
| [PATH](#the-provider-command-is-found-through-absolute-path-entries-only) | Providers resolve through absolute PATH entries only. |
| [Trust dialogs](#workspace-trust-dialogs-are-not-answered-unless-you-ask) | Folder-trust dialogs are not answered unless you ask. |
| [Station keys](#the-station-keeps-only-the-prefix-keys-it-documents) | The station removes tmux's default prefix bindings. |
| [Local files](#local-files-stay-private) | Sockets, drafts and text are kept private and sanitised. |
| [Proposals](#proposals-are-bounded) | Proposals are size and count bounded and never run unaccepted. |
| [`/finish`](#finish-reads-before-it-types) | `/finish` types only into an idle composer. |
| [Folder code](#the-mission-folder-cannot-supply-code-to-bridge) | The mission folder cannot supply code to Bridge. |

### Headless output handling

- Headless direct Codex `exec` commands use structured JSON events, including
  absolute executable paths and pinned model and effort arguments.
- Prompt detection reads assistant and provider error events. Completed tool output
  remains in the bounded operation log and does not become a new interactive
  prompt.
- Wrappers and non-Codex custom command templates retain raw heuristic scanning,
  because their output has no verified provider event boundary.

### Operator commands are refused for managed providers

Bridge sets `LLM_BRIDGE_MANAGED=1` in every provider it launches. While that
variable is set, `llm-bridge` runs only `status`, `events`, `task list`, `task
show`, `proposal create`, `proposal list`, `mission list`, `missions` and `help`.

- Anything else (`proposal accept`, `task approve`, `cancel`, `stop`, `steer`,
  `start`, `mission create`) exits with status 2 and `This command needs the
  operator; it is not available to the managed provider`.
- This is an advisory guard. The provider has the same UID as you and can edit the
  state database or unset the variable.
- The Bridge console and controller drop the variable, so operator commands keep
  working there.

### A managed provider can address only its own mission

Bridge also sets `LLM_BRIDGE_MISSION_ID` for the provider.

- While `LLM_BRIDGE_MANAGED=1`, a command whose `--mission` resolves to any other
  mission (by full ID or ID prefix) exits with status 2 and `A managed provider
  may only address its own mission`.
- When `--mission` is omitted, the provider's own mission is used instead of one
  inferred from the working folder.
- Read-only listings (`mission list`, `missions`) still show every mission's name
  and folder.
- A provider can unset the variables, send keys to the Bridge console through
  tmux, or read the SQLite database directly.

### Providers do not see each other's credentials

Each provider process loses the credential variables that belong to other
providers.

| Provider | Credential handling |
| --- | --- |
| Codex | Does not receive `CLAUDE_CODE_OAUTH_TOKEN`, `GH_TOKEN` or `ZAI_API_KEY`. Keeps `AZURE_*`. |
| Claude | Keeps `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_*`, and the AWS and Google groups (Bedrock and Vertex AI). |
| Copilot | Keeps the GitHub tokens. |
| Kilo and OpenCode | Also keep bring-your-own `ANTHROPIC_*`, `OPENAI_*`, z.ai and MiniMax keys, and all other groups except the registry tokens. |
| Any provider | `SSH_AUTH_SOCK` is kept for every provider, because providers run `git` over SSH. No provider keeps the registry tokens. |

- Verifiers receive no provider credentials.
- The table is `common.CREDENTIAL_GROUPS` and `common.PROVIDER_CREDENTIAL_GROUPS`.
  A provider it does not list is left unchanged.
- Besides the provider logins, the table covers `AWS_*`, `GOOGLE_*` and
  `GEMINI_*`, `AZURE_*`, `OPENROUTER_*`, `HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN`,
  and the package-registry tokens `NPM_TOKEN`, `NODE_AUTH_TOKEN`, `PYPI_TOKEN`,
  `TWINE_PASSWORD` and `CARGO_REGISTRY_TOKEN`.
- The station's tmux server starts without the credentials of providers the
  mission cannot use (those not enabled in the mission's saved configuration), so
  panes, the Bridge console and the mission picker do not inherit them.
- The launch script of each Native run also removes the credentials that belong to
  the other providers.
- Bridge adds its own package directory to `PYTHONPATH` for its `python -P -m`
  launches. Providers, verifiers and the tmux server do not receive it.
- Every process of your user can read the environment of your other processes
  (`/proc/<pid>/environ`) and the credential files under your home directory, such
  as `~/.claude`, which stay readable.

### Headless Codex and Claude prompts are not in the process arguments

The arguments of a running process can be read by every process of your user
(`/proc/<pid>/cmdline`, `ps`).

- A Headless or Background run of Codex (`codex exec -`) and Claude (`claude
  --print`) reads its prompt from stdin, redirected from the private `prompt.txt`
  (mode 0600) of the run directory.
- A Native run, an attached terminal, Kilo, OpenCode, Copilot and custom command
  templates still receive the prompt as an argument, because those commands need
  it there. The prompt stays visible to your other processes for the length of such
  a run.
- The prompt is also stored in the run directory and the mission database, which
  only your user can read.

### The provider command is found through absolute PATH entries only

Bridge starts providers inside your mission folder, so an empty or relative PATH
entry (`:`, `.`, `bin`) would let a file in that folder take the place of `codex`
or `claude`. Bridge resolves the command to an absolute path before the launch, and
the Native launch script exports a PATH without such entries.

### Workspace-trust dialogs are not answered unless you ask

A Headless or Background run that stops at a provider's "trust this folder" dialog
is aborted with a message that names the fix: trust the folder once in the provider
CLI, or create the mission with `--trust-folder`, which lets Bridge press Enter at
that dialog. `status` shows `trusts folder` for such a mission.

In every case (including plain `ralph-robin` and `llm-scheduler`):

- Enter is sent only when the dialog text is among the last three non-empty lines of
  output and nothing has been printed for half a second.
- Text that only appears earlier in the output, such as in a file the provider
  printed, is ignored.
- Blocking-prompt and rate-limit detection uses the same rule (last eight non-empty
  lines, quiet half a second).
- For Claude's stream-json output, tool calls and tool results are not scanned at
  all, so a file that contains a phrase such as "out of tokens" cannot abort a run
  when the provider reads it.

### The station keeps only the prefix keys it documents

tmux's default prefix table (87 bindings, including `:` for the command prompt) is
removed at station start.

- Only `d` (detach), `z` (zoom), `[` (copy mode), `B`/`b`, `P`/`p`, `M`/`m`, `?` and
  the prefix itself (send a literal prefix) remain.
- Default key bindings that open a command prompt or a menu, in the root, copy-mode
  and copy-mode-vi tables, are removed as well.
- Without this, text pasted into a provider pane as one burst such as Ctrl-] then
  `:` then a tmux command ran that command, and `run-shell` would run any program
  as you.

### Local files stay private

- When `$XDG_RUNTIME_DIR` is unavailable, tmux sockets use `/tmp/lb-<uid>`. Bridge
  accepts that directory only if it is a real directory (not a symlink) owned by
  you with mode 0700. Otherwise it uses `/tmp/lb-<uid>-1` to `-4`.
- If all five names are unusable, it uses `${XDG_CACHE_HOME:-~/.cache}/llm-tools/runtime`
  (private, owned by you, and short enough for a socket path) instead of failing.
- Task drafts are written to `drafts/` in the Bridge state directory (mode 0700),
  removed after the command is accepted. Files older than 14 days are deleted when
  the launcher, console or control view starts.
- Text from mission names, task titles and proposals has control and
  bidirectional-format characters removed before it reaches a terminal.

### Proposals are bounded

One proposal prompt is at most 64 KiB, and one operation can create at most 20
proposals. Proposals never run until you accept them.

### `/finish` reads before it types

`/finish` captures the provider pane and sends keys only when the composer looks
idle.

- A pane that shows an approval, trust or yes/no dialog is left alone, with
  `Provider is asking something`.
- Set `LLM_BRIDGE_FINISH_SKIP_CHECK=1` to skip the check for a provider whose screen
  the heuristics do not recognise.

### The mission folder cannot supply code to Bridge

- Bridge starts its own Python processes with `-P`, so a `llm_tools/` package or
  `sitecustomize.py` inside the mission folder is never imported.
- Folder names containing `#(` or `#{` are refused, and other `#` characters are
  escaped, because tmux would run or expand them.

## Testing with chaotic input (fuzzing)

Bridge is fuzzed with standard-library generators only (`random.Random(seed)`; no
Hypothesis or Atheris). The generators, fake curses windows and targets live in
`tests/fuzz/`.

| File | Contents |
| --- | --- |
| `generators.py` | Random Unicode (combining marks, wide CJK, emoji and ZWJ sequences, bidi controls, C0/C1 controls, lone surrogates, NUL, very long lines, CR/LF mixes), invalid UTF-8 bytes, key and escape streams (truncated CSI, nested, unterminated and split bracketed pastes, SGR mouse reports with absurd coordinates, function keys), terminal sizes and resize storms, console command lines, CLI arguments and damaged snapshots. |
| `harness.py` | A sandbox (private state directory, `HOME`, `PATH` with inert provider stubs, working directory), a curses window double that records every write outside its bounds, a fake clock for the escape and paste timers, a watchdog for hangs, and a database fingerprint. |
| `targets_pure.py`, `targets_ui.py`, `targets_cli.py` | One target per surface: input primitives, bracketed paste, text buffer, document viewer, Markdown, theme and config parsing, event summaries, text helpers, regular-expression timing, console keys/drawing/whole loop, document viewer loop, home screen keys and loop, paste into the home screen, mission picker, console command lines, CLI parser, store state machine and chaotic stored text. |

Each target checks invariants:

- no exception;
- no write outside the window;
- no command dispatched except by an explicit Enter or a confirming Y (or a
  composer Ctrl-S);
- a pasted block reaches its destination byte for byte;
- read-only commands leave the database unchanged;
- store transitions follow a reference model;
- wrapped or clipped rows never exceed the width in cells.

Run them like this:

```bash
python -m pytest -q -m fuzz                       # fixed seeds, about a minute
python -m pytest -q -m "not fuzz"                 # everything else
python scripts/bridge_fuzz.py --list              # the targets
python scripts/bridge_fuzz.py --seconds 600 --seed 5000000 --target console_loop
python scripts/bridge_fuzz.py --seconds 120       # every target, time shared equally
python scripts/bridge_fuzz.py --case console_loop:1234        # replay one seed
python scripts/bridge_fuzz.py --replay ~/.cache/llm-tools/bridge-fuzz/cases/X.json
```

- `--seconds` is the total budget shared equally by the selected targets.
- Every failing case is saved once per distinct failure as JSON (default
  `${XDG_CACHE_HOME:-~/.cache}/llm-tools/bridge-fuzz/cases/`, override with `--out`)
  with its target, seed, traceback and the generated inputs. The case reproduces
  from the seed.
- `tests/test_bridge_fuzz_pty.py` additionally feeds two seconds of seeded terminal
  noise and resize signals to the control view and the home screen in a real
  pseudo-terminal. It then requires the process to be alive, to leave on Ctrl-D with
  status 0, to print no traceback and to leave the database unchanged.
- Each defect the fuzzers found has a minimal regression test in
  `tests/test_bridge_fuzz_regressions.py`.

## Troubleshooting

Start with `llm-bridge doctor`; it checks this computer and prints a `Next` line.
Every error ends with a `see:` address that points at its entry in
[troubleshooting](troubleshooting.md).

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `INTERRUPTED` on the home screen, picker or `missions` | The mission's controller stopped (crash, kill or reboot). | `llm-bridge events --limit 20`, then `llm-bridge start --mission ID` (uses paid capacity). |
| `could not start: REASON` after `start` | The controller failed before it was ready. | Fix the reason, or change the rotation with `llm-bridge rotation --mission ID --set LIST`. |
| `Waiting for capacity` | Every rotation entry is out of capacity. | Wait for the reset shown, change providers with `rotation --set`, or see [Suspend until credits recover](#suspend-until-credits-recover). |
| `Waiting for capacity data` | A provider has no usage data (not signed in). | Sign in, or wait: Bridge launches anyway after the deadline (default 900 s). |
| `waiting for you` / `Needs you` | A task awaits review, or every unfinished task is blocked, failed or paused. | Run the command `status` names (`task approve`, `task retry`, `task resume`). |
| Task in `REVIEW` and nothing else starts (native mission) | The review hold. | Approve, reject or `/continue`; see [Native missions hold for review](#native-missions-hold-for-review). |
| `copilot is disabled by [usage].providers` | The rotation names a provider not enabled in config. | Add it to `[usage].providers` or set `LLM_USAGE_PROVIDERS`. |
| `schema-too-new` (exit 4) | The database was written by a newer llm-bridge. | Upgrade llm-tools or restore a backup; see [Downgrade](#database-upgrades-backups-and-downgrades). |
| `control needs an interactive terminal` (exit 2) | No terminal on stdin or stdout. | Use `llm-bridge status`, or `ssh -t`. |
| `prompt too large for the provider command line` | The prompt exceeds 131,071 bytes. | Shorten it or refer to a file in the working folder. |
| `verifier-modified` | A tracked verifier file changed. | Review the change and approve the task explicitly. |
| `This command needs the operator` (exit 2) | A managed provider ran an operator command. | Run it yourself. |
| Provider stopped on a trust dialog | The folder is not trusted by the provider CLI. | Trust it once in the provider CLI, or create the mission with `--trust-folder`. |
| Glyphs are not drawn correctly | The locale or terminal is not UTF-8. | Set `LLM_BRIDGE_ASCII=1`; see [Appearance](#appearance). |

For a bug report, attach the output of [`doctor --bundle`](#diagnostic-bundle-for-bug-reports)
after reading it.

## Reference

### Station reference

- The isolated tmux server uses Ctrl-] as its prefix, leaving the provider's usual
  Ctrl-B available as input.
- The station sets a 10 ms escape delay so Esc reaches the provider promptly,
  enables tmux extended keys so Shift-Enter and other modified keys reach providers
  that request them, and keeps 50000 lines of scrollback.
- When the provider exits, the pane shows a short message rather than tmux's "Pane
  is dead" banner.
- With clients of different sizes attached, the window follows the most recently
  active client.
- Clicks, paste, wheel, focus and resize events are forwarded to the provider when
  its mode requests them.

| Keys | Action |
| --- | --- |
| Ctrl-] then B / b | Open the full Bridge controls |
| Ctrl-] then P / p | Return to the same provider process |
| Ctrl-] then M / m | Open the mission picker |
| Ctrl-] then z | Toggle zoom of the provider pane |
| Ctrl-] then ? | Show the shortcut reminder |
| Ctrl-] then d | Detach |
| Ctrl-] then [ | Enter tmux copy mode (`q` leaves it) |
| Ctrl-] twice | Send a literal Ctrl-] to the provider |

- The status line shows `Bridge: Ctrl-] then B` on the left, `Provider: Ctrl-] then
  P` on the right and the current task context in the middle.
- While a task runs, the status context adds `no output for Nm` once the provider
  screen has been quiet for 30 seconds.

**Clipboard.**

- The station sets `set-clipboard external`. Text you copy in tmux copy mode
  reaches the local clipboard through OSC 52 in terminals that support it (many do;
  some need it enabled), including over SSH.
- An application in the provider pane cannot set the clipboard with its own OSC 52
  sequence.
- Without OSC 52 support nothing reaches the clipboard. Use Shift-drag selection or
  export.
- The viewer's `c` copy uses a local helper (`wl-copy`, `xclip`, `xsel` or `pbcopy`)
  and is separate from OSC 52.
- When mouse capture blocks terminal selection, try Shift-drag or tmux copy mode.

**Hiding the status row.** For a child that needs every terminal row:

1. Press `Ctrl-] :`, enter `set -g status off`, and press Enter.
2. Restore the status row with `Ctrl-] :` then `set -g status on`.

Bridge's `b`/`p` focus and `d` detach shortcuts still work while the status row is
hidden. An attached 80×24 acceptance session verified the child changes from 80×23
to 80×24 and back, and prefix focus changes preserve its process identity.

### Terminal scrollback

Bridge stores provider logs, not tmux scrollback. The isolated tmux server that
runs a station keeps scrollback in memory only, bounded per pane by
`history-limit`, which Bridge sets to 50,000 lines (`llm_tools/terminal.py`). It is
discarded when the pane or the station ends and is never written to the state
directory. Provider output that is kept on disk is the rotated log described above.

### Retention reference

**Event classes.** Every event kind belongs to one of three classes
(`llm_tools/bridge_events.py`: `AUDIT_SUMMARY_KINDS`, `AUDIT_RECORD_KINDS`,
`DIAGNOSTIC_KINDS`; a test requires each kind to be in exactly one).

| Class | Kinds | Retention |
| --- | --- | --- |
| Audit summary | `mission-created`, `mission-status`, `mission-reopened`, `task-verified`, `verification-failed`, `events-pruned`, `operation-log-pruned`, `controller-failed` and `controller-exited-unexpectedly` | Never deleted. |
| Audit record | What the operator or controller decided or did, for example task edits and proposals, `operation-exited`, `operation-uncertain`, provider handoffs and switches, rotation changes, steering requests, review holds, suspend decisions, `command-failed` and `command-rejected` | Kept while the mission is not finished and until `finished_missions_days` after it finishes. |
| Diagnostic | Bookkeeping that explains how the controller got there, for example `repository-checkpoint`, `prompt-assembled`, `operation-progress`, `operation-process`, `operation-prepared`, `provider-selection`, `task-status-transition`, `controller-config`, `station-observation` and the capacity, heartbeat and wake events | Deleted after `events_days`. |

A kind that this version does not know (an old database may hold one) is treated as
an audit record.

**Pruning pass details.**

- Each pass deletes in transactions of at most 2000 rows and stops after about half
  a second. If work remains, it continues 0.25 seconds later.
- A pass that removed anything writes one `events-pruned` event on the controller's
  own mission with the number removed per category (`events`,
  `finished_mission_events`, `operations`, `managed_processes`, `operation_logs`,
  `verifier_files`, `migration_backups`), the cutoff times and the first 20 missions
  affected. It writes one `events-pruned` event on each other mission that lost rows
  or files.
- Deletion of old diagnostic events and thinning of capacity observations happen on
  most passes, so they are audited at most once an hour (the counts accumulate in
  between and are written when the controller stops). Every other category is
  audited on the pass that removed it.
- Rows of an operation that is still active are never deleted.
- Event IDs keep their gaps, so replication cursors are not affected.
- The database file does not shrink. Freed pages are reused.

**Disk budget.** `scripts/bridge_retention_budget.py` writes the rows of provider
runs through the store (event payload sizes taken from the fake-provider end-to-end
tests) into a throwaway database and measures the file growth, including SQLite page
and index overhead. Measured with SQLite page size 4096:

| Row | Size |
| --- | --- |
| One provider run (task, operation, 12 events) | 8.7 KB |
| One `capacity-refresh` event (two providers, four windows) | 2.2 KB |
| One command record (`task.add`) | 1.6 KB |
| One `events-pruned` audit event | 0.4 KB |

The budget assumes **1 GiB for all Bridge state on a Pi's card**. This is an
assumption, not a value Bridge enforces. The projection is for one mission that is
never finished, running continuously for a year under the defaults:

- Events are full size for the last 90 days.
- Older events lose their diagnostic kinds, leaving the task row, the operation row
  and the three audit records of each run (3.7 KB per run instead of 8.7 KB). The
  script measures the 3.7 KB by running the retention rule over the rows it wrote.
- Capacity observations are one per 10 minutes for 90 days and one per hour before
  that.

| Component | One run every 10 minutes (52,560 runs) | One run every 3 minutes (175,200 runs) |
| --- | --- | --- |
| Run rows, last 90 days | 107 MiB | 357 MiB |
| Run rows, older | 140 MiB | 467 MiB |
| Capacity observations | 40 MiB | 40 MiB |
| Commands (30 days) | 6.5 MiB | 22 MiB |
| Audit events written by pruning itself (about 120 a day) | 18 MiB | 18 MiB |
| Heartbeats (last hour) | 0.2 MiB | 0.2 MiB |
| Database total | 312 MiB | 904 MiB |
| Provider run logs, worst case (20 runs, one output file each, 4 rotated files of 4 MiB) | 320 MiB | 320 MiB |
| State total, worst case | 632 MiB (62% of 1 GiB) | 1224 MiB (120%) |

- A run every 10 minutes around the clock fits with a wide margin.
- A run every 3 minutes fits only because the provider logs rarely reach their worst
  case. The database alone is 904 MiB (88%), and the logs bring the worst case over
  1 GiB.
- Creating the mission with `--log-max-bytes 1048576` lowers the log bound to 80 MiB
  and the worst case to 984 MiB (96%).
- Tasks, operations and audit records of a mission that is not finished are never
  pruned, so the database of such a mission still grows by about 3.7 KB per run (one
  task, one operation and three audit events). Beyond one run every 3 minutes,
  finish the mission or split the work so the finished-mission rules can apply.
- Finished missions shrink. After `events_days` their diagnostic events go, and after
  `finished_missions_days` only mission and task rows and a handful of summary
  events remain (a few KB per task).

### Appearance

**Themes.** Bridge applies semantic colour roles to headings, navigation, selected
rows, command input, success, warnings, errors and secondary text.

- It preserves the terminal's default background. Selected rows also use
  bold/reverse video so focus is visible without colour.
- The native provider continues to own its terminal colours and input.
- `LLM_BRIDGE_THEME=dark|light` selects a palette explicitly (`high-contrast`,
  `neutral` and `mono` are also accepted). Otherwise Bridge uses `COLORFGBG` or an
  OSC 11 answer.
- When neither reports the background, Bridge assumes dark but draws with the
  `neutral` palette (about 3.6:1 contrast on both white and black, bold for
  emphasis, dim default-foreground for secondary text) and shows a one-time `theme:
  dark (guessed)` note in the home screen footer.
- Use `LLM_TOOLS_COLOR_<ROLE>` to override a shared role with an SGR specification,
  for example `LLM_TOOLS_COLOR_ERROR='1;34'`.
- `NO_COLOR`, `TERM=dumb`, non-terminal output and truthy `LLM_USAGE_NO_COLOR`
  disable colour. If your shell sets `NO_COLOR`, use `env -u NO_COLOR llm-bridge` to
  request colour for one launch while keeping your shell preference unchanged.

Screenshots are renders of actual tmux captures (110×30) for an isolated mission
with three saved tasks:

- [Dark terminal](../img/llm-bridge-dark.png): the Overview with its Recent
  activity section.
- [Light terminal](../img/llm-bridge-light.png): the Tasks view with the selected
  task previewed below the list.

They were captured before the view rename, so their tabs still read Bridge and
Orders. No provider was started or executed to produce them.

**Mission colours.** Each mission has an identity colour, one of eight hues (teal,
azure, blue, indigo, violet, magenta, pink, slate) chosen from its ID.

- It appears as a `▌` chip before the mission name in the console header, the home
  list and the mission picker, and as the filled label in the tmux station's status
  row.
- The console header also shows the mission's short ID after the name, `(9a3c51e0)`,
  so two missions with the same name stay distinguishable without the colour. The ID
  is left out only on a very narrow terminal.
- `llm-bridge mission colour [--mission ID] N|auto` pins a hue (1-8) or returns to
  the automatic one.
- `[bridge.theme.roles] mission_1 = "#5f87ff"` in the llm-tools config changes a
  hue.
- `llm-bridge theme` previews the palette and shows which glyph set is active.

**State glyphs and ASCII locales.** Colour never stands alone. Every state also has
a glyph and a word.

| State | Glyph | ASCII |
| --- | --- | --- |
| RUNNING | `●` | `*` |
| QUEUED | `○` | `o` |
| REVIEW | `◆` | `?` |
| COMPLETED | `✓` | `+` |
| FAILED | `✕` | `!` |
| PAUSED | `‖` | `=` |
| CANCELLED | `⊘` | `-` |
| provider | | `@` |

- A locale of `C` or `POSIX` (set in `LC_ALL`, `LC_CTYPE` or `LANG`, for example
  `LANG=C` over SSH), a locale that is not UTF-8, or `LLM_BRIDGE_ASCII=1` gives the
  ASCII forms.
- The screens then draw `|` instead of `·`, `Up/Down` instead of `↑↓` and `...`
  instead of `…`.
- See the [colour guide](colour-guide.md).

### Environment variables used by Bridge

| Variable | Effect |
| --- | --- |
| `LLM_BRIDGE_STATE_DIR` | Use another state directory. |
| `LLM_BRIDGE_THEME` | `dark`, `light`, `high-contrast`, `neutral` or `mono`. |
| `LLM_BRIDGE_ASCII` | Force ASCII glyphs. |
| `LLM_BRIDGE_NO_SIGNIN_CHECK` | Turn off the offline "installed but not signed in" warning. |
| `LLM_BRIDGE_CODEX_APP_SERVER_CMD` | Override the read-only Codex observation command (local protocol fixtures). |
| `LLM_BRIDGE_FINISH_SKIP_CHECK` | Skip the `/finish` idle-composer check. |
| `LLM_BRIDGE_MANAGED` | Set to `1` by Bridge in every provider it launches. |
| `LLM_BRIDGE_MISSION_ID` | Set by Bridge to the provider's own mission. |
| `LLM_USAGE_PROVIDERS` | Providers every tool may use; overrides `[usage].providers`. |
| `LLM_USAGE_NO_COLOR`, `NO_COLOR` | Disable colour. |
| `LLM_TOOLS_COLOR_<ROLE>` | Override a shared colour role with an SGR specification. |
| `LLM_SCHEDULER_USAGE_JSON`, `LLM_SCHEDULER_USAGE_JSON_FILE` | Inject capacity snapshots that the controller and the Usage view read. |
| `VISUAL`, `EDITOR` | External editor for the composer (Ctrl-O). |
| `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`, `XDG_CACHE_HOME` | Base directories for state, tmux sockets and caches. |
| `TMUX`, `TMUX_PANE` | Removed by Bridge when it attaches from inside your own tmux. |
