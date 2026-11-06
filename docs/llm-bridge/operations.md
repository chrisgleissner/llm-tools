# LLM Bridge operations

LLM Bridge stores missions and task history locally. It connects task order,
provider operations, and verification evidence so an operator can inspect what
has happened before approving completion or retrying uncertain work.

## Install and create a mission

Install `llm-tools` as usual. Native and Background missions use a tmux
station, so they need `tmux`; Headless (`--mode headless`) runs a mission without it. The existing provider CLIs and credentials are
used by the scheduler. Bridge adds no provider SDK or network service.

Run `llm-bridge` with no arguments from any working directory to open the home
screen. It lists saved missions with status, short id (the id the CLI accepts
as `--mission`), name, age and folder; `●` marks a
mission whose folder is the current folder and `✕ INTERRUPTED` replaces the status of a RUNNING or PAUSED
mission whose controller stopped unexpectedly (crash, kill or reboot); the detail line gives the fix,
`llm-bridge start --mission ID`. With no
missions it explains what a mission is. Use Up/Down and Enter to choose a
mission, or **N** for a new mission; **?** opens help and **Q** quits.
The creation form defaults to the current folder and Native interactive. With
no configured rotation it selects the first provider that is installed and
signed in (Claude Code, then Codex, OpenCode, Copilot, Kilo), so a machine with
only one signed-in CLI starts with that one. For a Claude Code selection that
has never been started in the folder, the Start hint adds `Claude asks once to
trust this folder: choose Yes`. It offers only provider CLIs found on `PATH` (missing ones are labelled "not
installed") and, after them, routes from the llm-tools config whose launch CLI
is installed, each with its pinned model. The selected entries come first in
rotation order (`[1]` is on top), the others follow alphabetically; Space
toggles an entry. Each focused field shows a hint line. Tab/Enter moves
between fields, Space changes Native/Headless (Headless starts with
`--headless`), Ctrl-E opens the
multiline objective composer, and Escape keeps a private recovery draft. The
last row, **Start and open provider** (it says `Starts claude now · uses
provider capacity`, naming the first selected provider), saves the mission, starts its controller, then attaches to
its validated station. **Save mission only** comes before it and saves the first
task without starting provider work; repeated Enter walks the fields and stops
on Save, so Start needs Tab/Down and then Enter. After a save the home screen
stays open with the new mission selected and says `Saved f292f491 · S start ·
o controls`; **S** starts the selected created or stopped mission and **o**
opens its controls, which show `saved as f292f491` once. On a mission with a
live native station Enter attaches to the provider directly (detaching returns
to the list) and **o** opens the controls; any other mission opens its controls. After a native start returns from the station (detached or
provider exited), Bridge opens the mission's controls. The home form's
Headless choice runs the provider without a terminal and with no station. Ctrl-D leaves controls; Q leaves the home
screen. The home screen enables bracketed paste, so newlines inside a paste are
kept in the objective. For terminals without bracketed paste, an Enter that
arrives inside a burst of input is treated as pasted text: it never starts or
saves a mission, and on the Start or Save button it only shows a hint to press
Enter again. CLI mission creation remains save-only.
For a multiline objective use the composer or `mission create --file` as below.

The installed executable works outside the checkout. After upgrading an older
local editable installation that lacks the new commands, refresh it with
`pipx install --force --editable /path/to/llm-tools`; `command -v llm-bridge`
should resolve the pipx launcher. `llm-bridge doctor` checks private
storage integrity and tmux availability, and for each provider in the rotation
(the mission's with `--mission`, otherwise the default) whether its command is
on `PATH` and whether the sign-in files its CLI writes exist, for example
`codex: installed, logged in` or `claude: not on PATH`, and ends with a `Next`
line (install tmux, sign in, or the first-mission steps, including Claude Code's
one-time folder trust prompt). It reads local files
only and sends no request; an expired token is found when the provider runs. No-argument use without a terminal
prints help and does not create mission state.

## Dark and light terminal themes

Bridge applies semantic colour roles to headings, navigation, selected rows,
command input, success, warnings, errors and secondary text. It preserves the
terminal's default background; selected rows also use bold/reverse video so
focus is visible without colour. The native provider continues to own its
terminal colours and input.

`LLM_BRIDGE_THEME=dark|light` selects a palette explicitly (`high-contrast`, `neutral` and
`mono` are also accepted). Otherwise Bridge uses `COLORFGBG` or an OSC 11 answer.
When neither reports the background, Bridge assumes dark but draws with the
`neutral` palette (about 3.6:1 contrast on both white and black, bold for
emphasis, dim default-foreground for secondary text) and shows a one-time
`theme: dark (guessed)` note in the home screen footer. Use `LLM_TOOLS_COLOR_<ROLE>` to override a shared role with an SGR
specification, for example `LLM_TOOLS_COLOR_ERROR='1;34'`. `NO_COLOR`,
`TERM=dumb`, non-terminal output and truthy `LLM_USAGE_NO_COLOR` disable colour.
If your shell sets `NO_COLOR`, use `env -u NO_COLOR llm-bridge` to request colour
for one launch while keeping your shell preference unchanged.

[Dark terminal](../img/llm-bridge-dark.png) and
[light terminal](../img/llm-bridge-light.png) are renders of actual tmux
captures (110×30) for an isolated mission with three saved tasks: the dark one
shows the Overview with its Recent activity section, the light one shows the
Tasks view with the selected task previewed below the list. They were captured
before the view rename, so their tabs still read Bridge and Orders. No provider
was started or executed to produce them.

## Explicit mission creation

Create a mission from a file (or use `--prompt TEXT` for a short objective):

```bash
llm-bridge mission create --file objective.md --cwd "$PWD" --json
```

A provider receives the objective and task prompt as one command-line
argument, and Linux accepts at most 131,071 bytes in one argument. A prompt
that is larger is refused before the provider starts: the task is marked
`FAILED`, a `task-launch-refused` event records `prompt too large for the
provider command line (N bytes)`, and no other provider is tried. Shorten the
task, or put the detail in a file in the working folder and refer to it from
the prompt. A mission command template that reads `{prompt_file}` is not
subject to the limit.

The result includes the mission ID and initial task ID. `--json` only creates
the mission; start it separately:

```bash
llm-bridge start --mission MISSION_ID
```

A mission runs in one of three modes. The same three names appear in the home
form, `--help`, `llm-bridge help` and `status`:

| Mode | `--mode` | Provider | Station |
| --- | --- | --- | --- |
| Native | `native` | the provider's own interactive terminal UI | tmux station with the provider in its pane; open it with `attach` |
| Headless | `headless` | runs without a terminal; Bridge shows its activity output | none; no tmux needed, nothing to attach to |
| Background | `background` (CLI default) | runs without a terminal, controlled by a background controller | tmux station whose provider pane tails the controller log; the Bridge pane takes control commands |

The home form offers all three (Space or Left/Right switches) and starts on
Native, which is recommended for interactive work. The command line defaults to
Background because it needs no terminal, so a script behaves the same on every
machine. Older documentation and saved events call Background mode "autonomous".
Mission JSON carries `"mode": "native|background|headless"` in `config`, next to
the saved `headless` and `native_tui` booleans.

Pass `--mode native|background|headless` to `mission create` or `start`; a mode
on `start` overrides and saves the mission's mode. The older `--native` and
`--headless` flags still work as aliases of `--mode native` and `--mode
headless`. A flag that contradicts `--mode`, or `--native` with `--headless`,
is refused (`--headless and --native cannot be combined`). The
rotation comes from the llm-tools config (`[ralph]` before `[defaults]`),
else the built-in `claude,codex,opencode`, limited to the providers the config
enables. `mission create --help` prints the rotation a new mission gets now.
`--routes` names configured route IDs and wins over
`--providers`. An explicit `--providers` without `--routes` clears configured
routes, so the providers you list are what runs.

`mission create` and `start --providers/--routes` check every rotation entry
with the check `rotation --set` uses: a name that is neither a provider nor a
configured route is refused with a suggestion (`unknown rotation entry 'claud'
· did you mean claude?`) before anything is saved. An entry whose CLI is
installed but not signed in only produces a warning on stderr (`warning: codex
is not signed in (run: codex login); it will be skipped until it is`), read
offline from the same files as `doctor`; `LLM_BRIDGE_NO_SIGNIN_CHECK=1` turns the
warning off. `start` refuses a cancelled or completed mission with a message and
exit status 4 (wrong state), and prints `running` only after the controller has reported
ready; a controller that fails first gives `mission ID could not start: REASON
· fix it with: llm-bridge rotation --mission ID --set LIST`.

`mission create` warns on stderr when the folder already has an unfinished
mission (`warning: this folder already has unfinished mission 14f56611 "..."
(CREATED); llm-bridge task add --mission 14f56611 ... adds to it`) and still
creates the new one; `--new` suppresses the warning. The list printed when
folder inference is ambiguous shows each mission's last-updated time, and the
`Needs you` line and receipts spell out `--mission ID` so the command works as
copied.

`--mission` (short `-m`) accepts a full mission ID or a unique prefix of 3 or more
characters. When it is omitted, Bridge uses the one unfinished mission whose
folder is the current folder or a parent of it; a live mission wins over
stopped or failed ones. With no match, or several, the error lists candidates.
CLI commands print human-readable text; add `--json` (short `-j`) for the stable
machine-readable form.

### Command-line conventions

* **One list command.** `llm-bridge mission list` and `llm-bridge missions` print the same
  table: `ID`, `STATE`, `NEEDS YOU` (`review task 1` when a task awaits your review),
  `TASKS` (done/total), `NAME`, `FOLDER` and `UPDATED`, live missions first. The
  current provider and task are in `missions --json` (`provider`, `current_task`). `console` is
  an internal command and is not listed.
* **Task numbers.** A task is named by the number `task list` shows: `llm-bridge task approve 1`
  is the same as `llm-bridge task approve --task 1` (short `-t`). `--task-id ID` takes the global
  ID, which is internal; JSON task objects carry both `number` (an alias of `ordinal`, the
  number you type) and `id` (the global ID). `proposal accept N` and `proposal reject N` take the
  task number in the same way, and `llm-bridge view notes.md` reads a file without `--file`.
* **Options are spelled out.** A long option is never accepted as a prefix: `stop --f` is an
  error, not `stop --force`. Explicit aliases (`--prompt-file`, `-f`, `-m`, `-t`, `-j`) work.
* **Pagination.** Every list command takes `--limit N` and ends a partial list with
  `showing N of M <things> · --limit N shows more · <continuation>`: `events` (newest N,
  `--after EVENT_ID` reads forward), `task list` (first N, `--offset N` continues; `--all` lists
  every task), `proposal list` (`--after TASK_NUMBER`) and `mission list` / `missions` (live
  first; 100 in text, all with `--json`). `--json` lists are objects (`{"tasks": [...],
  "total": N, "offset": N}`, `{"missions": [...], "total": N}`, `{"events": [...]}`,
  `{"proposals": [...]}`, `{"workers": [...]}`), never bare arrays.
* **Coordinator URLs and `--target`.** `worker enroll --coordinator` and `remote login
  --coordinator` accept either `https://HOST:PORT` or `wss://HOST:PORT` for the same coordinator
  and store the scheme each command needs. `--target NAME` goes before the command (`llm-bridge
  --target build-1 mission list`); after the command it is an error that shows the corrected
  command line.

### Exit status and error output

| Status | Meaning |
| --- | --- |
| 0 | Success, or nothing to do (`Already paused`, `Already stopped`, `Nothing to pause`). |
| 1 | Runtime failure: a provider, the controller, the disk or the network failed. `doctor` exits 1 when no provider can run. |
| 2 | Usage error: unknown command or option, a value that is not valid, a command refused for a managed provider. |
| 3 | Not found or ambiguous: no such mission, task, ID prefix or folder mission, or a prefix that matches several; also `task output`/`task diff` when there is nothing to show. With `--target`, 3 also means the target did not answer (the request stays queued). |
| 4 | Wrong state: the mission or task is not in a state that allows the command (`start` on a cancelled mission, `task approve` on a task that is not in review, `finish` without a native provider, `attach` without a station). |

With `--json`, an error is printed on standard output (not standard error) as

```json
{"error": {"code": "mission-not-found", "message": "no mission matches 'zzzz'", "hint": "run llm-bridge mission list"}}
```

with the same exit status. `hint` is the command that fixes the problem, or an empty string.
The codes are `usage` (exit 2), `invalid-argument` (2), `mission-not-found` (3), `task-not-found`
(3), `not-found` (3), `mission-ambiguous` (3), `wrong-state` (4), `conflict` (4, a task changed
since you read it), `refused-in-managed-session` (2) and `failed` (1). The same table is in
`llm-bridge --help` under "Exit status".

Mission creation accepts `--verifier 'COMMAND'` (split like a shell, not run through one) or the command last after `--`, `--verifier-arg` (repeat once per argv element),
`--verifier-timeout` (default 300 seconds), `--log-max-bytes` (default 4 MiB),
`--log-rotations` (default 3), `--log-retained-runs` (default 20), and
`--capacity-refresh-seconds` (default 60). The verifier timeout range is
1–3600 seconds; log size is 1 KiB–1 GiB, rotations 0–20, retained runs 1–1000,
and capacity refresh 5–3600 seconds. These are mission options, not entries in
the shared TOML file. The only `[bridge]` table the TOML reader accepts is
`[bridge.theme]` (`name`, `background`, `roles`); see the
[colour guide](colour-guide.md).

List missions with `llm-bridge mission list`, and inspect one with:

```bash
llm-bridge status --mission MISSION_ID
llm-bridge events --mission MISSION_ID --limit 20
llm-bridge events --mission MISSION_ID --since 10m --follow
llm-bridge doctor --mission MISSION_ID
```

`events` shows the newest events (the last 100 by default), oldest first, in the
same words as the console's Activity view. Controller bookkeeping (heartbeats,
mode, prompt and wake records) is hidden unless `--verbose` is given. `--since 10m`
keeps only recent events (`90s`, `2h`, `1d`), `--follow` prints new events until
Ctrl-C, and `--after ID` reads oldest first from an event ID. Event IDs are
global across all missions, so a mission's first event is not ID 1. A
`command-rejected` event carries the reason the command was refused. `--json`
keeps the raw fields (`kind`, `data`) and adds the `total` of matching events.

`status` reports the mission, tasks and their states, recent operations,
pending commands, and recent events. `--json` selects machine-readable output;
it does not create, start, or otherwise change a mission. `doctor` reports the
state directory, SQLite integrity, and tmux availability, then checks every provider the home form offers (or the mission's own rotation with `--mission`). It exits 0 when at least one provider is installed and signed in, and 1 otherwise; `--json` adds `ready` (boolean) and `blocking` (list of reasons). The monitor reports
configured mode separately from actual controller mode. Actual mode is shown
only after a verified controller-mode event; provider liveness and observed
model remain `unknown` when no provider observation proves them. An operation's
model is a configured scheduler pin, not an observed model.

## Task ordering and verification

The task lifecycle uses `QUEUED`, `READY`, `RUNNING`, `VERIFYING`, `COMPLETED`,
`FAILED`, `BLOCKED`, `PAUSED`, and `CANCELLED`. Only `COMPLETED` tasks satisfy
dependencies. Provider exit code zero moves work to `VERIFYING`; completion
requires explicit operator approval or a passing configured verifier.

`VERIFYING` is the stored value (database, JSON `status`, remote protocol). Every
screen and table shows the same state as `REVIEW`, because the task is waiting for
you, and task JSON also carries `"status_label": "REVIEW"`. A mission with a task in
`REVIEW` shows `NEEDS YOU` instead of `RUNNING` in the console header, the station
strip, the home list and the picker.

Tasks are numbered within their mission: the first task of every mission is
`task 1`, numbers are assigned at creation and never reused. `--task N`,
`--depends-on`, `--add` and the console aliases (`/approve 2`, `/reject 2 REASON`)
all take that number, and receipts and errors use it. JSON output shows it as
`ordinal` next to the global `id`; `--task-id ID` selects a task by the global
ID. A number outside the mission is reported with the valid range, for example
`mission f292f491 has tasks 1–3; there is no task 5`. Databases created before
schema 7 are numbered on first open, by creation order within each mission.

Add a task with a verbatim file or standard input. Dependencies refer to task
numbers and prevent a task from becoming ready until each prerequisite is
completed. Higher priority runs first, then lower explicit order, then older
task:

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
tasks last. `RUNS` is the place of a task that still waits to run (1 runs next).
`PRIORITY` and `ORDER` are the two stored sort keys and they sort in opposite
directions: a higher priority number runs first (the default is 0, negative
numbers run last; there are no P0 to P3 urgency levels), and among equal
priorities a lower order number runs first. `task priority` and `task reorder`
say where the task now runs, for example `Priority of task 3 set to -5 · runs
after tasks 1 and 2`.

Amend task text with `llm-bridge task amend --mission ID --task N --file
revision.md`. Amendments preserve the earlier prompt and increment the task
revision. When no provider step is working on the task, the new text applies at
once (`Amended · task 2 is now revision 3`). While a provider is running the
task, the amendment waits for the end of that step; `task show` then lists it as
`pending amendment (applies after the current step)` next to the text still in
use. Since prior evidence may no longer apply, an amended task that was
awaiting approval is queued again and must be verified against its new
revision. Because that changes what would be approved, `task amend` refuses a task
that is in `REVIEW` unless you pass `--requeue`; the earlier result is then sent
back and the amended task runs again (uses capacity). A task title that came from
the first prompt line follows the new prompt.

`task add` on a completed mission reopens it, with or without `--mission` (the
folder's one completed mission is used when no unfinished one exists): the
mission becomes `CREATED` (not started yet), a `mission-reopened` event is
recorded and the receipt says `Mission ID reopened · start it with: llm-bridge
start --mission ID`. A cancelled mission takes no tasks.

A provider's successful exit moves its task to `REVIEW` (stored as `VERIFYING`); success alone does
not complete the task. `task verify --evidence TEXT` alone only records the
note, which `task show` lists with its source and age; the task stays in
`REVIEW`. Approve it explicitly, with optional evidence:

```bash
llm-bridge task approve --mission MISSION_ID --task N \
  --evidence "Reviewed implementation and test output"
```

To send a task back instead, give a reason. The task is queued for another
attempt and its next prompt quotes the reason. The retry starts at once when a
controller is running and uses paid capacity, and the receipt says so. Add
`--hold` to keep the task paused instead (resume it with `task resume`). In the
console, `/reject N REASON` asks for confirmation with two choices: `Y` rejects
and retries, `H` rejects and holds (the same as `--hold`):

```bash
llm-bridge task reject --mission MISSION_ID --task N --reason "tests were not run"
llm-bridge task reject --mission MISSION_ID --task N --reason "tests were not run" --hold
```

For deterministic checks, configure a verifier at mission creation. Write the
command as `--verifier 'python -m pytest -q'`, or after `--` at the end of the
command line (`-- python -m pytest -q`). Bridge splits it into words as a shell
would, does not invoke a shell, and prints the result as an argument list in the
creation receipt. For exact quoting, `--verifier-arg` is one argv item; repeat the
flag to form the command. Give only one of the three forms. The `{context}` placeholder names a private JSON file
with the objective, task prompt, revision, and acceptance criteria. Other
placeholders are `{cwd}`, `{mission_id}`, `{task_id}`, and `{revision}`. The
verifier succeeds when it exits zero. JSON output is optional: when present,
`summary` and an `artifacts` list are recorded, and `passed: false` vetoes
success. Artifact paths are checked relative to the mission working directory.
`--verifier-timeout` defaults to 300 seconds and
must be greater than zero and no more than 3600 seconds.

```bash
llm-bridge mission create --file objective.md --cwd "$PWD" \
  --verifier './verify-task {context}' --verifier-timeout 120 --json
```

The provider works in the same folder as the verifier and can edit it. When the
mission is created, Bridge records a SHA-256 of the verifier program and of the
script it interprets (for example `check.py` in `python check.py`) when they are
inside `--cwd`, plus any file named with `--verifier-file PATH` (repeatable,
relative to `--cwd`). If one of them has changed, been deleted or (for a
symlink) been repointed by the time a task is verified, Bridge does not run the
verifier and does not complete the task. The task stays in `REVIEW` with a
`verification-failed` event whose reason is `verifier-modified` and which lists
the changed paths, and you approve it explicitly. Programs outside `--cwd`
(such as `/usr/bin/python3`) are not tracked. Missions created before this
check have no recorded hashes and behave as before.

### Native missions hold for review

A native mission is interactive, so its controller does not start the next
task while a finished task awaits your decision. After a provider run ends with
task 1 in `REVIEW`, the controller waits (it records a `review-hold` event)
until you approve task 1 (`/approve 1`), reject it (`/reject 1 REASON`, which
queues it again), or release the hold with `/continue`. Only then does the next
task start. Background and headless missions are unattended and keep starting
the next task straight away.

* `continue` (`/continue` in the console, `llm-bridge continue [--mission ID]`)
  releases the hold once: the next ready task starts while the reviewed task
  stays in `REVIEW`. If that task later returns for review, or the reviewed
  task is rejected and comes back, the mission holds again. With no hold it
  reports `Nothing to continue` and changes nothing.
* `--auto-continue` on `mission create`, or `llm-bridge mission set
  --auto-continue on|off` for an existing mission, turns the hold off (or on).
  Turning it on while the controller is holding releases the hold at once.
* With a configured verifier, a passing verifier completes the task and nothing
  holds. A failing verifier leaves the task in review and holds.
* There is nothing to hold back when no other task can start (no further task,
  or the next one depends on the reviewed task). Then the task simply shows the
  usual `Needs you` approval.
* Approve, reject, continue and auto-continue each end the hold with a
  `review-hold-released` event (`how` is `approved`, `rejected`, `continue` or
  `auto-continue`; `summary` is a human sentence).

### Provider native teams and token spend preference

New missions default to provider-native team guidance enabled and Medium token
spend preference. Set global defaults in the shared `config.toml`:

```toml
[bridge]
teams = true
token_spend = "medium" # low | medium | high
```

Override either choice for one mission with `mission create --teams on|off`
and `--token-spend low|medium|high`. Change a saved mission with, for
example, `llm-bridge mission set --mission ID --teams off --token-spend low`.
`llm-bridge status --json` reports `mission.execution_policy`; readable status
and the station Overview show the effective settings. Explicit model, effort,
authorization, and user prompt choices keep their existing precedence.

The teams guidance asks for native subagents only when the provider supports
them and the work benefits from separation. Unsupported providers continue as
one agent. With teams disabled, every token spend preference omits Bridge-added
delegation guidance. Low minimizes extra passes and repeated context, escalating
when that cannot meet requirements. Medium adjusts reasoning and review to task
risk and uncertainty. High permits more capable reasoning and independent
skeptical checks when worthwhile. These preferences never weaken mandatory
validation or acceptance. Existing missions with no saved policy retain legacy
prompts until an operator sets either value; setting teams on for such a mission
uses Medium unless `--token-spend` is also supplied. Repeated task invocations
receive the same short policy prefix, so it adds prompt overhead each time.
User-supplied task and mission text is stored unchanged and appears after the
prefix.

The team-enabled policy is 104–106 whitespace-delimited words (750–773 UTF-8
bytes, depending on preference). These are measured text sizes, not tokenizer
counts. Each launch records `prompt-assembled` with incoming, assembled and
Bridge-added UTF-8 byte counts, plus total wrapper bytes relative to distinct
source content when assembled by the controller. Token count remains unknown without verified
provider usage. Identical task and mission objective content appears once in
the assembled context; distinct task content and stored original text are
preserved. No equivalent direct-provider run has established marginal token
overhead or parity.

While holding, `status` prints `Needs you   task 1 awaits your review ·
/approve 1 or /reject 1 reason · /continue to start the next task now` and
`Holding     the next task has not started for 3m · task 1 awaits your review`.
The snapshot carries `hold` (`{"reason": "review", "task_ordinal": 1, "tasks":
[1], "since": ISO time}`, or `null`) and the same text in `needs_you` (`kind`
`approval`, `hold: true`). In the station, the provider pane's footer after a
clean provider exit reads `Provider exited (status 0) · Task 1 ready for
review · Ctrl-] B then /approve` (`/reject` is listed in the controls); tmux formats that
footer when the process exits, so the controller sets it before launching a
native run (it is omitted when a verifier is configured or auto-continue is
on). A provider pane that is still an empty placeholder shows the same
sentence as its placeholder text.

If no verifier is configured, the task remains in `REVIEW` for operator
review. A failing verifier or timeout also leaves it uncompleted. See
`llm-bridge task verify --help` and `llm-bridge mission create --help` for the
full flag list.

## Console and control screen

The compact console is available in the tmux station. It has a command entry
line and refreshes mission status in the remaining six lines. Type a command
and press Enter; `help` lists the console commands. The `›` command line
accepts slash commands as well as bare commands: `/help [TOPIC]`, `/start`
(opens the provider station in native mode), `/attach` (also works from inside
your own tmux), `/approve [N]` (approve task N; N may be omitted when exactly
one task is in `REVIEW`), `/continue` (start the next task while a reviewed one stays in `REVIEW`), `/pause`, `/resume`, `/stop`, `/cancel`, `/status`,
`/tasks` and `/events`. Typing `/` opens a list of the slash commands with a
one-line description each; it narrows as you type. Tab completes a single
match, or only the common prefix of several (a second Tab names the matches),
so an ambiguous prefix is never completed to the first command. An unknown
command is answered with close matches. `cancel`, `stop --force`, task cancellation and
approval ask for a compact confirmation: **Y** confirms, **N** or **Esc**
cancels. The Overview's `Needs you` row names tasks awaiting approval.
Console commands include mission controls (`pause`, `resume`, `stop`,
`cancel`), task controls (`verify N`, `reject N REASON`, `priority N VALUE`,
`reorder N VALUE`, `dependencies N PREREQUISITE_N`, `retry N`, `pause N`,
`resume N`, `cancel N`), and task text changes (`add FILE`, `amend N FILE`),
where N is the per-mission task number. File paths with spaces can be quoted.
Control commands use the same handlers as the CLI.

Open the full terminal UI directly with:

```bash
llm-bridge control --mission MISSION_ID
```

It offers nine views on F1–F9: Overview, Tasks, Providers, Activity,
Sessions, Health, Actions, Usage and Agents. The names used before the rename (Bridge,
Orders, Stations, Log, Systems, Palette) are still accepted wherever a view is
named. A status-coloured header names the
mission and its state, and a footer lists the keys that apply to the current
view (`?` opens help for the current view). The Overview summarizes mission state, current work,
task counts, acceptance-criteria progress and a `Needs you` row, and a
`Recent` section fills the remaining rows with the latest mission events (time,
kind, summary). The Tasks view lists tasks with a priority column (`P5`) and
shows a live preview of the selected task below the list: title, revision,
acceptance criteria, evidence and the start of the prompt. The preview follows
the selection; `v` opens the complete prompt and Enter inspects the task. While a task
is running and the provider screen has been quiet for 30 seconds, its Now row
and the station status line show `no output for 2m`. Capacity is phrased in
plain language, for example `5h 66% left, lasts past reset, resets 2h 5m`;
"lasts past reset" means the current pace will not exhaust the window before it
resets. The Activity view collapses heartbeats into readable summaries, and the Health,
Providers, Sessions, Actions and Usage views use aligned columns. Tasks are fetched in pages of 200; Page Up
and Page Down navigate them, and Ctrl-F opens the Tasks filter. Search and
page controls make tasks past the first page reachable without loading the
whole queue into the terminal process. Activity and Sessions views show recent
bounded histories.

`control` needs a terminal on both stdin and stdout. Without one (a pipe, a
script, `ssh` without `-t`) it prints `llm-bridge: control needs an interactive
terminal; use llm-bridge status` and exits 2. `llm-bridge usage` prints the
`llm-usage` table once instead, so a script can read it.

A mistyped command or option gets one line and exit status 2 (an error object on standard
output with `--json`), for example
`llm-bridge: unknown command 'stauts' · did you mean status? · llm-bridge --help
lists commands`. Internal commands (`console`, `_controller`, `_station-gc`) are
not listed or suggested.

The Agents view shows roots across missions and exact parent-bound Codex children.
Use `llm-bridge agents --mission ID` for one mission or `--all-missions` for the
combined CLI map. Managed providers can inspect only their own mission. Root age
measures the current Bridge operation; child age uses the provider's session
creation timestamp. Known completion freezes elapsed age. Missing creation or
completion metadata stays unknown. Age ticks locally without a model call.

The inspector distinguishes observed model/effort from configured values and
unknown fields. Codex's read-only persisted-thread API can report `notLoaded`
even while a worker is doing work in another runtime. Bridge retains that
native status separately from observed task lifecycle and labels bounded,
exact-session activity as last observed, with its
timestamp and partial or stale freshness. It reads tool names and lifecycle
tags, never tool arguments, results or hidden reasoning. Other providers expose
Bridge roots only; their native child enumeration is currently unavailable.
Enter switches only to an existing live Bridge station where supported;
otherwise it opens the inspector. No navigation action resumes or forks an agent.
`LLM_BRIDGE_CODEX_APP_SERVER_CMD` overrides the read-only observation command for
local protocol fixtures; ordinary use discovers the installed Codex CLI.

The Usage view retains the usage renderer's cell colours through Bridge's
palette, including separate red, yellow, green and cyan guidance. Explicit
`NO_COLOR`, `LLM_USAGE_NO_COLOR` and monochrome terminals still suppress colour.
Text and column fitting use the same dashboard as `llm-usage`.
The status line names the source (`live llm-usage read`). By default the view lists only the
mission's rotation providers; `a` shows every enabled provider whose CLI is installed. Under a
capacity injection (`LLM_SCHEDULER_USAGE_JSON`, `LLM_SCHEDULER_USAGE_JSON_FILE`) the view renders the
same snapshots the controller reads. When a live read still differs from the controller's last
observation by more than 5 percentage points for a window, a line under the status gives the
controller's figures and their age. An exhausted weekly or monthly window reads `× empty`.

Keyboard and mouse operate those views:

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

The composer keeps recoverable source in private state. Ctrl-O launches the
external editor as argv on a private file; returning from that editor does not
submit the task. Ctrl-S explicitly saves the composed source. Escape or Ctrl-D closes
the composer and keeps the draft (`Esc close (draft kept)`); Ctrl-E in the
console reopens the newest kept task draft of the last 24 hours with `Restored
draft from HH:MM · Ctrl-X discard`; Ctrl-X is the only key that discards. An empty
task composer shows dim guidance on the first line (title, description, optional
`Acceptance:` list). The footer shows a line count, and the byte count only above
80% of the 1 MiB limit. Help pages (`?`, `/help`) open as a word-wrapped page
with `↑↓ scroll · / find · Esc close` and none of the file viewer's controls; `?`
on the home screen shows a short home-screen key summary, and `T` there opens the
quickstart. Rejected saves keep their source. The UI accepts printable command text without
using letters as view shortcuts. Actions and keyboard commands use the
same dispatcher. The command result pane summarizes durable command envelopes
as a concise applied/rejected message; detailed state remains available in the
Activity view, CLI status, and event history. The station follows terminal resize
events and is designed to remain usable down to 80 columns by 24 rows.

For long initial objectives, Ctrl-E in the launcher opens the full-screen
composer. Enter inserts a newline and Ctrl-S saves to the form. Escape retains a
private recovery draft that can be reopened with Ctrl-E. The interactive limit is
1 MiB of UTF-8 source bytes; an oversized paste is rejected atomically. Use
`mission create --file PATH` or `--file -` for larger inputs. In the document
viewer, `/` searches, `n`/`N` repeat, `m` toggles source/rendered text, `w` toggles
wrapping, Left/Right scroll long source lines when wrapping is off, and `s`
exports the complete original source to a new path. Source export never copies a
rendered or abbreviated preview.

Built-in help works offline: `llm-bridge help` lists every topic with a one-line
description (quickstart, terms, keys, console, prompts, markdown, providers,
states, verification, recovery, remote and distributed; the list lives in
`bridge_help.TOPIC_SUMMARIES` and feeds every other list of topics). The top-level
`llm-bridge --help` groups the commands (Start here, Missions, Tasks, Run control,
Station, Capacity and power, Data, Distributed, Appearance) and lists the global
options `--version` and `--target WORKER`. Each command page has one example and,
for commands that change something, an `Effects:` line, for example `start` uses
paid capacity and `suspend-until-credits` suspends this computer. Type `/help` in
the console for the slash commands generated from the same groups, with the shell
command each shortcut stands for. `llm-bridge theme` ends with how to set the
theme (`--palette-size` and `--colour` replace the older `--colors` and `--color`).
`llm-bridge rotation --set LIST` is the one documented way to change a rotation;
`--routes` and `--providers` still work. A rendered document uses Rich when
available; source viewing and CLI help still work if Rich is unavailable.

Providers distinguishes the station selected for viewing from the last station
selected to execute work. Selecting a station with Enter or running
`llm-bridge station --mission ID NAME` changes only the view preference; it
does not change routing, start a provider, or switch an executing operation.
Models are labelled as configured or observed so a model pin is never mistaken
for provider-reported identity. The Health view keeps configured mode and
actual controller mode separate; `unknown` means there is no verified
observation.

The compact console and full control view accept the same command handlers.
The compact pane's six status lines include the current mission/task summary.
Its `help` command lists task and mission controls. Typed steering is available
in either UI and on the CLI:

| Kind | Effect |
| --- | --- |
| `replace TASK FILE` | Replace a task prompt at the next safe boundary |
| `review TASK [FILE]` | Queue a review task, optionally from a prompt file |
| `verify TASK` | Request the configured verifier for a task awaiting verification |
| `pause-after-iteration` | Pause after current iteration, including while waiting for provider capacity |
| `stop-after-task TASK` | Stop after the selected task reaches completion |
| `handoff ROUTE` | Prefer the named configured route at the next selection |

CLI form: `llm-bridge steer --mission ID --kind KIND`, with `--task N`
(the per-mission task number; `--task-id ID` takes the global ID),
`--to PROVIDER` (older name `--route`), and `--file PATH` or `--prompt TEXT` as
required by that kind. `llm-bridge steer --help` lists only `handoff` and
`review`. The other kinds still work for older scripts, but have their own
commands: `task amend` (replace), `task verify` (verify), `pause`, and
`pause --after-task` and `stop --after-task [N]` for the two boundary kinds.
`pause --after-task` stores the same intent as `steer --kind pause-after-iteration`
(the mission pauses when the provider's current invocation ends, also during a
wait for capacity). `stop --after-task [N]` stores the same intent as
`steer --kind stop-after-task --task N`; without N it waits for the task that
is running now. It cannot be combined with `--force`. The steer spellings remain
hidden aliases.
See `llm-bridge steer --help` and `steer help` in the console for the live
syntax.

## Attach, pause, stop, and resume

Attach to the private tmux station with `llm-bridge attach --mission ID` and
leave your own terminal with Ctrl-] then d. `llm-bridge detach --mission ID` is
for another shell: it detaches the station's single connected writable client,
which is usually a different terminal from the one running the command. With
no client attached it says so; with several attached it refuses and lists each
terminal with its size and idle time (read-only observers are marked), so you
can choose with `--client /dev/pts/N`. On success it prints `Detached
/dev/pts/18 from mission f292f491`. Read-only observers are never detached. After you detach with Ctrl-] then d, `llm-bridge attach` prints `Detached from
mission f292f491 (name) · it keeps running · llm-bridge attach to return`
instead of tmux's `[detached (from session mission-…)]` line (a finished
mission says `the mission is completed` and so on). `attach` runs tmux with its
stdout captured, because tmux prints that line to stdout and draws the screen
through the terminal device; any other tmux exit line, such as `[server
exited]`, is passed through unchanged. Detaching only
closes the terminal view; it does not pause the mission or stop a provider. Headless missions have no station to
attach to. When no station is running, `attach` says so and names
`llm-bridge start --mission ID`.

The isolated tmux server uses Ctrl-] as its prefix, leaving the provider's
usual Ctrl-B available as input. The station sets a 10 ms escape delay so Esc
reaches the provider promptly, enables tmux extended keys so Shift-Enter and
other modified keys reach providers that request them, keeps 50000 lines of
scrollback, and sets `set-clipboard external`: text you copy in tmux copy mode
reaches the local clipboard through OSC 52 in terminals that support it (many
do; some need it enabled), including over SSH. An application in the provider
pane cannot set the clipboard with its own OSC 52 sequence. Without OSC 52
support nothing reaches the clipboard; use Shift-drag selection or export. The
viewer's `c` copy uses a local helper (`wl-copy`, `xclip`, `xsel` or `pbcopy`)
and is separate. With clients of different sizes attached, the window
follows the most recently active client. When the provider exits, the pane
shows a short message rather than tmux's "Pane is dead" banner. Prefix B/b opens the full Bridge controls; P/p
returns to the same provider process; z toggles zoom of the provider pane; ? shows the
shortcut reminder. Press Ctrl-] then d to detach; Ctrl-] twice sends a literal
prefix to the provider. The status line shows `Bridge: Ctrl-] then B` on the left, `Provider:
Ctrl-] then P` on the right and the current task context in the middle.
Clicks, paste, wheel, focus, and resize events are forwarded to the provider
when its mode requests them. Detaching leaves the controller and panes alive.
When mouse capture blocks terminal selection, try Shift-drag or tmux copy mode.

`pause` prevents a new provider iteration from starting. If a provider is
already working, it runs until that invocation returns, then the controller
waits for `resume`. When provider capacity is unavailable, Ralph's managed
readiness wait keeps the machine awake and remains interruptible by default. To
opt in to RTC-backed sleep during an all-rate-limited wait, run
`llm-bridge mission set --mission ID --suspend-when-rate-limited on`. Bridge
sleeps only when no operation is active and its existing suspend path arms a
wake. Pause, stop and cancel changes made during sleep take effect after wake.
While the managed wait is awake, pause, stop, cancel, and boundary steering
interrupt it promptly. Separately, Bridge's background capacity-observation worker emits a fresh read-only
capacity snapshot every 60 seconds by default, configurable with
`--capacity-refresh-seconds`; those provider reads do not run on the terminal
input path. A user or external power manager can still put the machine to
sleep independently.
`resume` continues an active controller or starts one if needed.
`stop` never cancels a mission. With no controller (a saved mission that is
`CREATED`, `STOPPED` or `PAUSED`) it changes nothing and says `Nothing to stop ·
mission ID is saved (CREATED) · start it with: llm-bridge start` (`Already stopped
· ...` when it is `STOPPED`). `pause` on a mission that is not running likewise says
`Nothing to pause · mission is not running (CREATED)`, and on a paused one `Already
paused · mission ID is PAUSED`. These no-ops exit 0. With a controller, `stop` asks it to stop at its next
boundary and closes the owned station after the controller stops; in native mode it waits for the provider
invocation to return and says how to finish if that takes a while; a later resume creates or reconnects the
station as needed. `stop --force` reconciles unresolved work and terminates
only a process group or tmux station whose persisted ownership can be
validated. When no provider process was running it prints `Stopped · no provider
process was running` and exits 0; it fails only when a recorded provider process
cannot be confirmed as the mission's, and then names its PID. The task that
`stop --force` interrupted is recorded as `interrupted by stop --force`, and the
next `start` runs it again.

While a provider's capacity cannot be determined (not signed in, no usage data),
the task stays READY and no operation is open: `status` shows `Waiting for
capacity data · claude has no usage data, launches anyway in 14m (at 19:50)`.
The deadline is fixed when the wait starts (`--max-unavailable-wait`, default
900 s), so the countdown only goes down. After it passes, Bridge launches the
provider anyway. `cancel` durably cancels the mission and its unfinished tasks.
It ends the mission for good (`resume` and `start` refuse a cancelled mission), so
it asks `This cannot be undone` on a terminal and needs `--yes` when there is no
terminal to ask on, for example `llm-bridge cancel --mission ID --yes`. The
console's own dialog offers `Cancel mission` and `Keep mission`. A repeated
`pause` or `stop` on a mission that is already paused or stopped says `Already
paused` or `Already stopped`. `llm-bridge help quickstart` has a table of all the
ways to stop work.

After a controller restart, Bridge reconciles any operation that was in
progress. It may mark the operation uncertain and the task blocked for review;
it does not treat a vanished process or a provider's zero exit as proof that
the task is complete, and it does not silently replay uncertain work. Inspect
`status` and `events`, then use explicit verification, task retry, or mission
resume after resolving the uncertainty.

The provider pane in native mode is a real provider terminal UI while that
process is running. Session continuation is provider-specific and is not
inferred from terminal text or timestamps:

* For Claude Code and Copilot, Bridge uses its own UUID only when the installed
  CLI advertises `--session-id`. It records that session as `NATIVE_CREATED`
  only after structured provider metadata confirms the same UUID and working
  directory. On recovery it requests that exact ID only when the persisted
  Bridge-owned ID has the same provider and working directory and the CLI
  still advertises exact-ID resume. It reports `RESTORED_NATIVE` only after
  metadata confirms the resumed identity. Capability drift, missing metadata,
  or a directory mismatch prevents exact restoration.
* Bridge cannot choose a new Codex session ID. When the installed CLI supports
  the owned-process launch mode, Bridge inspects writable transcript descriptors
  in the validated provider process tree and accepts exactly one structured
  session identity with the expected directory. It never selects a transcript
  by recency. Unsupported capture, ambiguity, or missing metadata leaves exact
  continuity unavailable. Old records without explicit Bridge ownership are
  ignored. Exact resume requires the saved ID, directory, provider metadata,
  and current CLI capability to validate.

`RESTORED_NATIVE`, `NATIVE_CREATED`, `RECONSTRUCTED`, and `UNAVAILABLE` are
distinct records; do not treat a newly created native session as a restored
conversation. The Claude/Copilot recovery path has hermetic fake-provider
coverage. Live native restoration has not been verified for those providers.
The repaired bounded Codex probe verifies native reply, full controller stop,
exact-ID restoration by a new controller, and context recall; see
`final-native-mission.json` and the review ledger. When exact continuity is
unavailable, the next prompt is reconstructed from durable mission objective, task revision, checkpoints,
operation records, and verification evidence.

## Disabled providers, waiting and finished stations

- `[usage].providers` in the llm-tools config (or `LLM_USAGE_PROVIDERS`) lists
  the providers every tool may use. `mission create`, `start`, `resume` and
  `rotation --set` refuse a rotation whose launch CLI is not listed, for
  example `copilot is disabled by [usage].providers in ~/.config/llm-tools/config.toml
  · add it there or set LLM_USAGE_PROVIDERS`. An existing mission is checked
  against the config frozen into it. A rotation typed by the operator is
  refused when any entry is disabled; a default or saved rotation is refused
  only when no entry is left (the controller drops disabled entries and
  continues with the rest, as ralph-robin does).
- When a controller fails, a `controller-failed` event records a one-line
  reason. `status` prints `Controller  failed — <reason>` and the snapshot
  carries it as `lifecycle.failure_reason`. `status` also prints
  `stopped — run: llm-bridge start`, `paused — run: llm-bridge resume` or
  `finished` instead of the last heartbeat for missions without a live controller.
- When every rotation entry is out of capacity the snapshot has a `waiting`
  object (`reason`, `providers`, `until_epoch`, `detail`, `entries`) and
  `status` prints `Waiting     for capacity · copilot resets in 2d 9h · change
  providers with: llm-bridge rotation --set LIST`. In native mode an empty
  provider pane shows `Waiting for capacity: …`; the scheduler replaces that
  pane when a provider launches. A pane that still holds a previous
  provider's output is not overwritten.
- A COMPLETED, CANCELLED or FAILED mission closes its tmux station when no
  client is attached. If a client is attached, the station closes when the
  last client detaches (tmux `client-detached` hook runs the hidden
  `llm-bridge _station-gc --mission ID`). A native provider process that is
  still running keeps the station. `llm-bridge stop` still closes a retained
  station by hand.
- The snapshot's `operator_events` lists the latest 20 events that are not
  controller bookkeeping (heartbeats, capacity refreshes, process and
  checkpoint records); `events` is unchanged.

## Remote operation (SSH)

Missions, the controller and the tmux station belong to your user account, not
to a login session. Log in over SSH (or open another terminal) to find a
running mission, inspect it and attach without disturbing the local session:

```bash
llm-bridge mission list            # "INTERRUPTED (was RUNNING)" marks a mission whose controller is gone
llm-bridge status --mission 3f9a   # unique ID prefix of 3+ characters
llm-bridge attach --mission 3f9a   # open the provider station
# Ctrl-] then d detaches your terminal; mission and local session continue
```

Bridge looks for the station socket in `$XDG_RUNTIME_DIR/llm-bridge`,
`/run/user/<uid>/llm-bridge` and `/tmp/lb-<uid>`. An SSH login without
`XDG_RUNTIME_DIR` therefore still finds a station that was created from a
desktop session. Several terminals may be attached at once. tmux sizes the
window to the most recently active client, so the local view can be resized
while a remote client of a different size is typing (the station sets tmux
`window-size latest`). To avoid the resize, detach the other client (Ctrl-]
then d in that terminal, or `llm-bridge detach --mission ID` from a shell) or
use terminals of the same size. `llm-bridge help remote` says the same.
Detach with Ctrl-] then d, which removes only your own terminal. `llm-bridge` and
`llm-bridge control --mission ID` open the home screen and controls over SSH.

Copies you make in tmux copy mode use OSC 52, so they reach your local
clipboard when your SSH terminal supports OSC 52. The provider cannot set the
clipboard itself. Terminals
that block it fall back to Shift-drag selection or tmux copy mode (Ctrl-] [).

## Running Bridge inside your own tmux

You can run `llm-bridge`, `llm-bridge control` or the console command `/attach`
from a pane of your own tmux session. tmux refuses to attach from inside
another tmux, so Bridge attaches to its private station with `TMUX` and
`TMUX_PANE` removed for that call. The station appears nested in your pane and
Ctrl-] is the station prefix; your own tmux prefix is unchanged outside it.
Ctrl-] then d detaches the station and returns you to your pane. Station key
hints appear only inside a Bridge-owned station, not in other tmux sessions.

## Local state and config

Mission state defaults to
`${XDG_STATE_HOME:-~/.local/state}/llm-tools/llm-bridge`. Set
`LLM_BRIDGE_STATE_DIR` to use another directory. Bridge stores its SQLite
database, private runtime files, and logs there. Shared `config.toml` supports
`[bridge]` team and execution defaults plus `[bridge.theme]` appearance settings;
choose providers, routes, and execution mode on mission creation/start commands.
The selected shared tool configuration is
captured for the mission so later changes do not silently alter its routing.

Bridge tmux sockets use a short private runtime path under
`${XDG_RUNTIME_DIR}/llm-bridge` when available, or `/tmp/lb-UID`, with a
mission-ID hash in the socket name. Controller runtime files remain under the
private Bridge state directory. Provider output is rotated at 4 MiB per file
with three rotations by default. Bridge retains the latest 20 run directories;
when older directories are pruned it records log size and a SHA-256 digest in
the mission database before deleting the files. Adjust these limits with the
mission creation flags above.

The controller records a heartbeat every few seconds and a capacity observation
every minute. To keep the database bounded, it deletes heartbeats older than
one hour, thins capacity observations older than one hour to one per 10 minutes,
and records an `events-pruned` event for each pass that removed rows. The newest
heartbeat and observation are always kept. Task, operation, verification and
operator events are never pruned. An observation identical to the previous one
updates that row instead of adding another. The database file does not shrink
after pruning; the freed space is reused (run `VACUUM` while no mission is
running to return it).

See `llm-bridge --help` and the command-specific `--help` output for all
available CLI operations.

Read a local textual artifact with `llm-bridge view notes.md` (`--file notes.md` also works). Use
`--follow` explicitly for live output: Up pauses, End resumes; new output and
file rotation/truncation are labelled. Retention remains bounded, and pruned
content is unavailable. Without a TTY, `view` prints original source. Home `?`
opens the same searchable help viewer and returns to the launcher on close.

For a child that needs every terminal row, press `Ctrl-] :`, enter
`set -g status off`, and press Enter. Restore the status row with `Ctrl-] :`
then `set -g status on`. Bridge's `b`/`p` focus and `d` detach shortcuts still
work while the status row is hidden. An attached 80×24 acceptance session
verified the child changes from 80×23 to 80×24 and back, and prefix focus changes
preserve its process identity.

## Mission colours, state glyphs and ASCII locales

Each mission has an identity colour, one of eight hues (teal, azure, blue, indigo, violet,
magenta, pink, slate) chosen from its ID. It appears as a `▌`
chip before the mission name in the console header, the home list and the
mission picker, and as the filled label in the tmux station's status row. The
console header also shows the mission's short ID after the name, `(9a3c51e0)`,
so two missions with the same name stay distinguishable without the colour (the
ID is left out only on a very narrow terminal).
`llm-bridge mission colour [--mission ID] N|auto` pins a hue (1-8) or returns
to the automatic one. `[bridge.theme.roles] mission_1 = "#5f87ff"` in the
llm-tools config changes a hue. `llm-bridge theme` previews the palette.

Colour never stands alone. Every state also has a glyph and a word:
`● RUNNING`, `○ QUEUED`, `◆ REVIEW`, `✓ COMPLETED`, `✕ FAILED`,
`‖ PAUSED`, `⊘ CANCELLED`. A locale of `C` or `POSIX` (set in `LC_ALL`, `LC_CTYPE`
or `LANG`, for example `LANG=C` over SSH), a locale that is not UTF-8, or
`LLM_BRIDGE_ASCII=1` gives ASCII forms (`*` running, `o` queued, `?` review,
`+` completed, `!` failed, `-` cancelled, `=` paused, `@` for a provider), and
the screens draw `|` instead of `·`, `Up/Down` instead of `↑↓` and `...`
instead of `…`. `llm-bridge theme` shows which glyph set is active. See the
[colour guide](colour-guide.md).

The home list orders live missions first, then waiting ones (created, stopped,
failed), then finished ones (dimmed), and re-reads the database every two
seconds. At narrow widths the folder, then the provider, are dropped first.
`/` (or Ctrl-F) filters by name, folder or ID prefix; Enter opens the match and
Esc clears the filter. A mission with a task awaiting your review (`REVIEW`)
shows `◆ NEEDS YOU` instead of `● RUNNING`, in the home list and in the picker, and
its detail line says `task N awaits your review · Enter to open`.

The mission picker (`llm-bridge missions --pick`, or Ctrl-] then M in a station)
counts a mission as live only while it is not finished and has a running
controller or station. A finished mission whose tmux station is still running
shows `station idle` (muted). With such a mission selected, **x** (or Ctrl-X)
asks for confirmation (**Y**/**N**) and then closes that station through the same
safe path the controller uses: a station whose provider process is alive, or to
which a terminal is attached, is kept. `llm-bridge gc [--json]` does the same for
every finished mission. A RUNNING, PAUSED or STOPPING mission without a running
controller shows `INTERRUPTED` in the picker as on the home screen.

Choosing another mission in the picker moves your terminal to that station. The
station you left keeps running, and one detach (Ctrl-] then d) from the new
station returns you to where you started the first one (your shell or the home
screen).

## Capacity phrasing, safety and limits

### Suspend until credits recover

Use `llm-bridge suspend-until-credits --mission ID`, or choose **Suspend until
credits recover** in Actions. Bridge refreshes the selected rotation's capacity
and requires a genuine future reset. A balance with no scheduled reset, missing
usage, or an already usable rotation cannot supply a recovery wake time. The
command reports a refusal with its reason when it cannot establish a safe plan.

Automatic capacity sleep is opt-in with `mission set --mission ID
--suspend-when-rate-limited on`. Keep the mission controller running: its
background capacity worker also arms a wake while waiting for known credit
recovery, even when the mission is paused or the UI is detached. This arming
happens while the computer is awake, so a subsequent manual OS suspend already
has a wake planned. A detached UI alone does not provide this service; the
controller must remain alive. Arming a wake does not unpause a mission.

Wake requests are coordinated across missions. The earliest eligible request
owns the next wake; withdrawing one request preserves other owners. The shared
power backend arms the wake before requesting sleep, and verifies timer
availability. RTC readback is stronger evidence than a user timer: a systemd
WakeSystem timer is checked for activity, and actual wake reliability is
measured from the resumed wall clock. Firmware operations must hold a sleep
block inhibitor and the device locks. A provider or uncertain active device
operation prevents the explicit sleep action.

External hardware tools must hold an OS sleep block inhibitor throughout a
critical operation. Bridge checks these generic OS inhibitors alongside typed
managed operation and process identity; application device locks remain the
hardware tool's responsibility.

Intent, target, arm result, sleep/wake outcome and drift are mission-correlated
events. Simulated outcomes are labelled as such; they do not establish hardware
wake reliability. Host firmware, OS power support and a functioning wake backend
remain prerequisites. Unknown recovery times keep the computer awake.

For a retrospective, correlate the mission and operation IDs with the suspend
cycle ID and wake unit. Controller startup records the source snapshot and
process identity. Provider results record normalized failure causes and native
versus scheduler exit status when the transport supplies them; retained output
is identified by its private path, byte count and digest. Capacity observations
include their source, freshness and next eligibility. Power records distinguish
awake waits, timer arming, accepted sleep requests, observed sleep and subsequent
work. An accepted suspend command alone proves neither sleep nor resumed model
work. Existing private log rotation and retention still apply.

### Capacity display and task verification

Capacity lines use plain language, for example
`5h 66% left, lasts past reset, resets 2h 5m`. "Lasts past reset" means the
current pace will not exhaust the window before it resets. A background worker
refreshes provider capacity every 60 seconds by default. Controllers share
those reads through a private cache under
`${XDG_CACHE_HOME:-$HOME/.cache}/llm-tools/shared-usage` (one file and lock per
provider): with several missions running, a provider is queried about once a
minute in total, not once per mission. The next task selection of a mission
still reads the provider live.

`pause` does not sleep the machine, and Bridge adds no network listener. A
zero provider exit leaves a task in `REVIEW` until an operator approves it
(`/approve`, `task approve`) or a configured verifier passes. A
bounded live probe verified Codex native reply, controller stop, exact-ID
restoration and context recall. Claude and Copilot live restoration is
unverified.

## Provider trust boundary

Headless direct Codex `exec` commands use structured JSON events, including
absolute executable paths and pinned model/effort arguments. Prompt detection
reads assistant and provider error events; completed tool output remains in
the bounded operation log and does not become a new interactive prompt.
Wrappers and non-Codex custom command templates retain raw heuristic scanning,
because their output has no verified provider event boundary.

Providers run as your user, in your mission folder. Bridge narrows what a
provider can do by habit or by prompt injection; it does not sandbox it.

* **Operator commands are refused for managed providers.** Bridge sets
  `LLM_BRIDGE_MANAGED=1` in every provider it launches. While that variable is
  set, `llm-bridge` runs only `status`, `events`, `task list`, `task show`,
  `proposal create`, `proposal list`, `mission list`, `missions` and `help`.
  Anything else (`proposal accept`, `task approve`, `cancel`, `stop`,
  `steer`, `start`, `mission create`) exits with status 2 and
  `This command needs the operator; it is not available to the managed provider`.
  This is an advisory guard, not a security boundary: the provider has the same
  UID as you and can edit the state database or unset the variable. The Bridge
  console and controller drop the variable, so operator commands keep working
  there.
* **A managed provider can address only its own mission.** Bridge also sets
  `LLM_BRIDGE_MISSION_ID` for the provider. While `LLM_BRIDGE_MANAGED=1`,
  a command whose `--mission` resolves to any other mission (by full ID or ID
  prefix) exits with status 2 and
  `A managed provider may only address its own mission`. When `--mission` is
  omitted, the provider's own mission is used instead of one inferred from the
  working folder. Read-only listings (`mission list`, `missions`) still show
  every mission's name and folder.

  Both rules are guardrails, not a boundary. A provider runs as your user, so
  it can unset the variables, send keys to the Bridge console through tmux, or
  read the SQLite database directly. A real boundary needs a separate OS user or
  a sandbox (container, `bwrap`, or a VM) for the provider process.
* **Providers do not see each other's credentials.** Each provider process
  loses the credential variables that belong to other providers (for example
  Codex does not receive `CLAUDE_CODE_OAUTH_TOKEN`, `GH_TOKEN` or
  `ZAI_API_KEY`; Claude keeps `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_*`;
  Copilot keeps the GitHub tokens; Kilo and OpenCode also keep bring-your-own
  `ANTHROPIC_*`, `OPENAI_*`, z.ai and MiniMax keys). Verifiers receive no
  provider credentials. The table is `common.CREDENTIAL_GROUPS` and
  `common.PROVIDER_CREDENTIAL_GROUPS`; a provider it does not list is left
  unchanged. Besides the provider logins, the table covers `AWS_*`,
  `GOOGLE_*` and `GEMINI_*`, `AZURE_*`, `OPENROUTER_*`, `HF_TOKEN` and
  `HUGGING_FACE_HUB_TOKEN`, and the package-registry tokens `NPM_TOKEN`,
  `NODE_AUTH_TOKEN`, `PYPI_TOKEN`, `TWINE_PASSWORD` and `CARGO_REGISTRY_TOKEN`.
  Claude keeps the AWS and Google groups (Bedrock and Vertex AI), Codex keeps
  `AZURE_*`, Kilo and OpenCode keep all of them except the registry tokens, and
  no provider keeps the registry tokens. `SSH_AUTH_SOCK` is kept for every
  provider, because providers run `git` over SSH.
  The station's tmux server starts without the credentials of providers the
  mission cannot use (those not enabled in the mission's saved configuration),
  so panes, the Bridge console and the mission picker do not inherit them. The
  launch script of each Native run also removes the credentials that belong to
  the other providers. Bridge adds its own package directory to `PYTHONPATH` for
  its `python -P -m` launches; providers, verifiers and the tmux server do not
  receive it.
  This is hygiene, not a boundary. Every process of your user can read the
  environment of your other processes (`/proc/<pid>/environ`) and the
  credential files under your home directory, such as `~/.claude`, which stay
  readable.
* **Headless Codex and Claude prompts are not in the process arguments.** The
  arguments of a running process can be read by every process of your user
  (`/proc/<pid>/cmdline`, `ps`). A Headless or Background run of Codex
  (`codex exec -`) and Claude (`claude --print`) reads its prompt from stdin,
  redirected from the private `prompt.txt` (mode 0600) of the run directory. A
  Native run, an attached terminal, Kilo, OpenCode, Copilot and custom command
  templates still receive the prompt as an argument, because those commands
  need it there; the prompt stays visible to your other processes for the
  length of such a run. The prompt is also stored in the run directory and the
  mission database, which only your user can read.
* **The provider command is found through absolute PATH entries only.** Bridge
  starts providers inside your mission folder, so an empty or relative PATH
  entry (`:`, `.`, `bin`) would let a file in that folder take the place of
  `codex` or `claude`. Bridge resolves the command to an absolute path before
  the launch and the Native launch script exports a PATH without such entries.
* **Workspace-trust dialogs are not answered unless you ask.** A Headless or
  Background run that stops at a provider's "trust this folder" dialog is
  aborted with a message that names the fix: trust the folder once in the
  provider CLI, or create the mission with `--trust-folder`, which lets Bridge
  press Enter at that dialog. `status` shows `trusts folder` for such a mission.
  In every case (including plain `ralph-robin` and `llm-scheduler`) Enter is sent
  only when the dialog text is among the last three non-empty lines of output and
  nothing has been printed for half a second. Text that only appears earlier in
  the output, such as in a file the provider printed, is ignored. Blocking-prompt
  and rate-limit detection uses the same rule (last eight non-empty lines, quiet
  half a second). For Claude's stream-json output, tool calls and tool results
  are not scanned at all, so a file that contains a phrase such as "out of
  tokens" cannot abort a run when the provider reads it.
* **The station keeps only the prefix keys it documents.** tmux's default
  prefix table (87 bindings, including `:` for the command prompt) is removed at
  station start. Only `d` (detach), `z` (zoom), `[` (copy mode), `B`/`b`, `P`/`p`,
  `M`/`m`, `?` and the prefix itself (send a literal prefix) remain. Default key
  bindings that open a command prompt or a menu, in the root, copy-mode and
  copy-mode-vi tables, are removed as well. Without this, text pasted into a
  provider pane as one burst such as Ctrl-] then `:` then a tmux command ran
  that command, and `run-shell` would run any program as you.
* **Local files stay private.** When `$XDG_RUNTIME_DIR` is unavailable, tmux
  sockets use `/tmp/lb-<uid>`. Bridge accepts that directory only if it is a
  real directory (not a symlink) owned by you with mode 0700; otherwise it uses
  `/tmp/lb-<uid>-1` to `-4`. If all five names are unusable, it uses
  `${XDG_CACHE_HOME:-~/.cache}/llm-tools/runtime` (private, owned by you, and
  short enough for a socket path) instead of failing. Task drafts are written to `drafts/` in the Bridge
  state directory (mode 0700), removed after the command is accepted, and files
  older than 14 days are deleted when the launcher, console or control view
  starts. Text from mission names, task titles and proposals has control and
  bidirectional-format characters removed before it reaches a terminal.
* **Proposals are bounded.** One proposal prompt is at most 64 KiB, and one
  operation can create at most 20 proposals. Proposals never run until you
  accept them.
* **`/finish` reads before it types.** It captures the provider pane and sends
  keys only when the composer looks idle. A pane that shows an approval, trust
  or yes/no dialog is left alone, with `Provider is asking something`. Set
  `LLM_BRIDGE_FINISH_SKIP_CHECK=1` to skip the check for a provider whose
  screen the heuristics do not recognise.
* **The mission folder cannot supply code to Bridge.** Bridge starts its own
  Python processes with `-P`, so a `llm_tools/` package or `sitecustomize.py`
  inside the mission folder is never imported. Folder names containing `#(` or
  `#{` are refused, and other `#` characters are escaped, because tmux would
  run or expand them.

## Station details

The station sets a 10 ms escape delay so Esc is not delayed, and holds 50000
lines of scrollback. While a task runs, the status context adds `no output for
Nm` once the provider screen has been quiet for 30 seconds. `Ctrl-]` then `[`
enters tmux copy mode (`q` leaves it), and `Ctrl-]` twice sends a literal
Ctrl-] to the provider. The Bridge viewer's `c` copy uses a local helper
(`wl-copy`, `xclip`, `xsel` or `pbcopy`) and is separate from OSC 52.

## Testing with chaotic input (fuzzing)

Bridge is fuzzed with standard-library generators only (`random.Random(seed)`;
no Hypothesis or Atheris). The generators, fake curses windows and targets live
in `tests/fuzz/`:

* `generators.py`: random Unicode (combining marks, wide CJK, emoji and ZWJ
  sequences, bidi controls, C0/C1 controls, lone surrogates, NUL, very long
  lines, CR/LF mixes), invalid UTF-8 bytes, key and escape streams (truncated
  CSI, nested, unterminated and split bracketed pastes, SGR mouse reports with
  absurd coordinates, function keys), terminal sizes and resize storms, console
  command lines, CLI arguments and damaged snapshots.
* `harness.py`: a sandbox (private state directory, `HOME`, `PATH` with inert
  provider stubs, working directory), a curses window double that records every
  write outside its bounds, a fake clock for the escape and paste timers, a
  watchdog for hangs, and a database fingerprint.
* `targets_pure.py`, `targets_ui.py`, `targets_cli.py`: one target per surface
  (input primitives, bracketed paste, text buffer, document viewer, Markdown,
  theme and config parsing, event summaries, text helpers, regular-expression
  timing, console keys/drawing/whole loop, document viewer loop, home screen
  keys and loop, paste into the home screen, mission picker, console command
  lines, CLI parser, store state machine and chaotic stored text).

Each target checks invariants: no exception, no write outside the window, no
command dispatched except by an explicit Enter or a confirming Y (or a
composer Ctrl-S), a pasted block reaches its destination byte for byte, read
only commands leave the database unchanged, store transitions follow a
reference model, and wrapped or clipped rows never exceed the width in cells.

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

`--seconds` is the total budget shared equally by the selected targets. Every
failing case is saved once per distinct failure as JSON (default
`${XDG_CACHE_HOME:-~/.cache}/llm-tools/bridge-fuzz/cases/`, override with
`--out`) with its target, seed, traceback and the generated inputs; the case
reproduces from the seed. `tests/test_bridge_fuzz_pty.py` additionally feeds
two seconds of seeded terminal noise and resize signals to the control view and
the home screen in a real pseudo-terminal, then requires the process to be
alive, to leave on Ctrl-D with status 0, to print no traceback and to leave the
database unchanged. Each defect the fuzzers found has a minimal regression test
in `tests/test_bridge_fuzz_regressions.py`.
