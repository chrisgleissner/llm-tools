# LLM Bridge operations

LLM Bridge stores missions and task history locally. It connects task order,
provider operations, and verification evidence so an operator can inspect what
has happened before approving completion or retrying uncertain work.

## Install and create a mission

Install `llm-tools` as usual. The console station needs `tmux`; `--headless`
can run a mission without it. The existing provider CLIs and credentials are
used by the scheduler. Bridge adds no provider SDK or network service.

Run `llm-bridge` with no arguments from any working directory to open the home
screen. Use Up/Down and Enter to choose a mission, or **N** for a new mission.
The creation form defaults to the current folder, Codex, and Native interactive.
Tab/Enter moves between fields, Space changes Native/Autonomous, Ctrl-E opens the
multiline objective composer, and Escape keeps a private recovery draft. The
primary action, **Start and open provider**, saves the mission, starts its
controller, then attaches to its validated station. **Save mission only** saves
the first task without starting provider work. Opening an existing mission only
opens controls. Autonomous runs headless provider work and has no station. Ctrl-D
leaves controls; Q leaves the home screen. CLI mission creation remains save-only.
For a multiline objective use the composer or `mission create --file` as below.

The installed executable works outside the checkout. After upgrading an older
local editable installation that lacks the new commands, refresh it with
`pipx install --force --editable /path/to/llm-tools`; `command -v llm-bridge`
should resolve the pipx launcher. `llm-bridge doctor --json` checks private
storage integrity and tmux availability. No-argument use without a terminal
prints help and does not create mission state.

## Dark and light terminal themes

Bridge applies semantic colour roles to headings, navigation, selected rows,
command input, success, warnings, errors and secondary text. It preserves the
terminal's default background; selected rows also use bold/reverse video so
focus is visible without colour. The native provider continues to own its
terminal colours and input.

`LLM_BRIDGE_THEME=dark|light` selects a palette explicitly. Otherwise Bridge
uses `COLORFGBG`, falling back to dark when the terminal does not report a
background. Use `LLM_TOOLS_COLOR_<ROLE>` to override a shared role with an SGR
specification, for example `LLM_TOOLS_COLOR_ERROR='1;34'`. `NO_COLOR`,
`TERM=dumb`, non-terminal output and truthy `LLM_USAGE_NO_COLOR` disable colour.
If your shell sets `NO_COLOR`, use `env -u NO_COLOR llm-bridge` to request colour
for one launch while keeping your shell preference unchanged.

[Dark terminal](../img/llm-bridge-dark.png) and
[light terminal](../img/llm-bridge-light.png) show actual installed control UIs
with an isolated acceptance mission. No provider execution is implied by those
screenshots.

## Explicit mission creation

Create a mission from a file (or use `--prompt TEXT` for a short objective):

```bash
llm-bridge mission create --file objective.md --cwd "$PWD" --json
```

The result includes the mission ID and initial task ID. `--json` only creates
the mission; start it separately:

```bash
llm-bridge start --mission MISSION_ID
```

By default this opens a private tmux station. Its provider pane shows the
autonomous activity log; the Bridge pane accepts control commands. Use
`--native` during mission creation or `start` to run the selected provider's
interactive terminal UI in the provider pane. Use `--headless` during creation
or `start` to omit tmux. The modes `--native` and `--headless` cannot be used
together. The default provider order is `claude,codex,opencode`; set
`--providers` or `--routes` when creating the mission to choose a different
selection.

Mission creation accepts `--verifier-arg` (repeat once per argv element),
`--verifier-timeout` (default 300 seconds), `--log-max-bytes` (default 4 MiB),
`--log-rotations` (default 3), `--log-retained-runs` (default 20), and
`--capacity-refresh-seconds` (default 60). The verifier timeout range is
1–3600 seconds; log size is 1 KiB–1 GiB, rotations 0–20, retained runs 1–1000,
and capacity refresh 5–3600 seconds. These are mission options, not entries in
the shared TOML file. The TOML reader does not support a `[bridge]` table.

List missions with `llm-bridge mission list`, and inspect one with:

```bash
llm-bridge status --mission MISSION_ID
llm-bridge events --mission MISSION_ID --after 0 --limit 100
llm-bridge doctor --mission MISSION_ID
```

`status` reports the mission, tasks and their states, recent operations,
pending commands, and recent events. `--json` selects machine-readable output;
it does not create, start, or otherwise change a mission. `doctor` reports the
state directory, SQLite integrity, and tmux availability. The monitor reports
configured mode separately from actual controller mode. Actual mode is shown
only after a verified controller-mode event; provider liveness and observed
model remain `unknown` when no provider observation proves them. An operation's
model is a configured scheduler pin, not an observed model.

## Task ordering and verification

The task lifecycle uses `QUEUED`, `READY`, `RUNNING`, `VERIFYING`, `COMPLETED`,
`FAILED`, `BLOCKED`, `PAUSED`, and `CANCELLED`. Only `COMPLETED` tasks satisfy
dependencies. Provider exit code zero moves work to `VERIFYING`; completion
requires explicit operator approval or a passing configured verifier.

Add a task with a verbatim file or standard input. Dependencies refer to task
IDs and prevent a task from becoming ready until each prerequisite is
completed. Higher priority runs first, then lower explicit order, then older
task ID:

```bash
llm-bridge task add --mission MISSION_ID --file follow-up.md \
  --priority 5 --depends-on 1 --acceptance "tests pass"
llm-bridge task list --mission MISSION_ID
llm-bridge task show --mission MISSION_ID --task TASK_ID
llm-bridge task priority --mission MISSION_ID --task TASK_ID --value 10
llm-bridge task reorder --mission MISSION_ID --task TASK_ID --value 20
llm-bridge task dependencies --mission MISSION_ID --task TASK_ID --add PREREQUISITE_ID
llm-bridge task retry --mission MISSION_ID --task TASK_ID
llm-bridge task pause --mission MISSION_ID --task TASK_ID
llm-bridge task resume --mission MISSION_ID --task TASK_ID
llm-bridge task cancel --mission MISSION_ID --task TASK_ID
```

Amend task text with `llm-bridge task amend --mission ID --task ID --file
revision.md`. Amendments preserve the earlier prompt and increment the task
revision. Since prior evidence may no longer apply, the amended task must be
verified against its new revision.

A provider's successful exit moves its task to `VERIFYING`; success alone does
not complete the task. Approve it explicitly and record optional evidence:

```bash
llm-bridge task verify --mission MISSION_ID --task TASK_ID \
  --approve --evidence "Reviewed implementation and test output"
```

For deterministic checks, configure a verifier at mission creation. Each
`--verifier-arg` is one argv item; Bridge does not invoke a shell. Repeat the
flag to form the command. The `{context}` placeholder names a private JSON file
with the objective, task prompt, revision, and acceptance criteria. Other
placeholders are `{cwd}`, `{mission_id}`, `{task_id}`, and `{revision}`. The
verifier succeeds when it exits zero. JSON output is optional: when present,
`summary` and an `artifacts` list are recorded, and `passed: false` vetoes
success. Artifact paths are checked relative to the mission working directory.
`--verifier-timeout` defaults to 300 seconds and
must be greater than zero and no more than 3600 seconds.

```bash
llm-bridge mission create --file objective.md --cwd "$PWD" \
  --verifier-arg ./verify-task --verifier-arg '{context}' \
  --verifier-timeout 120 --json
```

If no verifier is configured, the task remains in `VERIFYING` for operator
review. A failing verifier or timeout also leaves it uncompleted. See
`llm-bridge task verify --help` and `llm-bridge mission create --help` for the
full flag list.

## Console and control screen

The compact console is available in the tmux station. It has a command entry
line and refreshes mission status in the remaining six lines. Type a command
and press Enter; `help` lists the console commands. These include mission
controls (`pause`, `resume`, `stop`, `cancel`), task controls (`verify ID`,
`priority ID N`, `reorder ID N`, `dependencies ID DEPENDENCY_ID`, `retry ID`,
`cancel ID`), and task text changes (`add FILE`, `amend ID FILE`). File paths
with spaces can be quoted. Control commands use the same handlers as the CLI.

Open the full terminal UI directly with:

```bash
llm-bridge control --mission MISSION_ID
```

It offers eight views: Bridge, Orders (Tasks), Stations (Providers/routes),
Systems, Log, Sessions, Palette, and Usage. The Bridge view summarizes mission state, current work, task counts,
and acceptance-criteria progress. Orders are fetched in pages of 200; Page Up
and Page Down navigate them, and Ctrl-F opens the Orders filter. Search and
page controls make tasks past the first page reachable without loading the
whole queue into the terminal process. Log and Sessions views show recent
bounded histories. Keyboard and mouse operate those views:

| Input | Action |
| --- | --- |
| Tab / Shift-Tab, Left / Right, F1–F8 | Change view while body focus is active |
| Up / Down, PgUp/PgDn, Home/End | Navigate the selected list or open document |
| Click a view label | Change view |
| Click a palette row, then Enter | Select and run that command |
| Wheel up / down | Move the selection |
| Page Up / Page Down in Orders | Previous / next 200-task page |
| Ctrl-F, type query, Enter | Filter Orders by task title or prompt; Escape cancels and restores the command draft |
| Orders: `v` | Open the selected task's complete prompt |
| Body Ctrl-E | Compose a new multiline task |
| Orders body Ctrl-R | Compose an amendment of the selected order |
| Composer Ctrl-O | Open `$VISUAL` or `$EDITOR` |
| Composer F2 / Ctrl-S | Save source; Enter inserts a newline |
| Bridge body `o` | Open the complete objective |
| Palette `/` | Filter labelled actions; single click only selects |
| Enter | Run the command in the input line |
| Ctrl-P / Ctrl-N | Previous / next command history item |
| Ctrl-U / Ctrl-K | Clear input or delete to end |
| Ctrl-G | Focus the current command draft |
| Escape | Leave command input and retain its draft |
| Ctrl-D / Ctrl-C | Exit when the input line is empty |

The composer keeps recoverable source in private state. Ctrl-O launches the
external editor as argv on a private file; returning from that editor does not
submit the task. F2/Ctrl-S explicitly saves the composed source. Escape retains
the draft, while explicit discard is separate. Rejected saves keep their source. The UI accepts printable command text without
using letters as view shortcuts. Palette actions and keyboard commands use the
same dispatcher. The command result pane summarizes durable command envelopes
as a concise applied/rejected message; detailed state remains available in the
Log view, CLI status, and event history. The station follows terminal resize
events and is designed to remain usable down to 80 columns by 24 rows.

For long initial objectives, Ctrl-E in the launcher opens the full-screen
composer. Enter inserts a newline and F2/Ctrl-S saves to the form. Escape retains a
private recovery draft that can be reopened with Ctrl-E. The interactive limit is
1 MiB of UTF-8 source bytes; an oversized paste is rejected atomically. Use
`mission create --file PATH` or `--file -` for larger inputs. In the document
viewer, `/` searches, `n`/`N` repeat, `m` toggles source/rendered text, `w` toggles
wrapping, Left/Right scroll long source lines when wrapping is off, and `s`
exports the complete original source to a new path. Source export never copies a
rendered or abbreviated preview.

Built-in help works offline: `llm-bridge help TOPIC` supports quickstart, keys,
prompts, markdown, providers, states, verification, and recovery. Type `help` in
the console for the same topic guidance. A rendered document uses Rich when
available; source viewing and CLI help still work if Rich is unavailable.

Stations distinguishes the station selected for viewing from the last station
selected to execute work. Selecting a station with Enter or running
`llm-bridge station --mission ID NAME` changes only the view preference; it
does not change routing, start a provider, or switch an executing operation.
Models are labelled as configured or observed so a model pin is never mistaken
for provider-reported identity. The Systems view keeps configured mode and
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

CLI form: `llm-bridge steer --mission ID --kind KIND`, with `--task ID`,
`--route NAME`, and `--file PATH` or `--prompt TEXT` as required by that kind.
See `llm-bridge steer --help` and `steer help` in the console for the live
syntax.

## Attach, pause, stop, and resume

Attach to the private tmux station with `llm-bridge attach --mission ID` and
remove attached clients with `llm-bridge detach --mission ID`. Detaching only
closes the terminal view; it does not pause the mission or stop a provider.
Headless missions have no station to attach to.

The isolated tmux server uses Ctrl-] as its prefix, leaving the provider's
usual Ctrl-B available as input. Prefix B/b opens the full Bridge view; P/p
returns to the same provider process; z toggles the split view; ? shows the
shortcut reminder. Press Ctrl-] then d to detach; Ctrl-] twice sends a literal
prefix to the provider. The status line labels Provider and Bridge targets.
Clicks, paste, wheel, focus, and resize events are forwarded to the provider
when its mode requests them. Detaching leaves the controller and panes alive.
When mouse capture blocks terminal selection, try Shift-drag or tmux copy mode.

`pause` prevents a new provider iteration from starting. If a provider is
already working, it runs until that invocation returns, then the controller
waits for `resume`. When provider capacity is unavailable, Ralph's managed
readiness wait keeps the machine awake and remains interruptible. Separately,
Bridge's background capacity-observation worker emits a fresh read-only
capacity snapshot every 60 seconds by default, configurable with
`--capacity-refresh-seconds`; those provider reads do not run on the terminal
input path. Pause, stop, and boundary steering interrupt the managed wait
promptly. Bridge does not automatically suspend the operating system; a user
or external power manager can still put the machine to sleep independently.
`resume` continues an active controller or starts one if needed.
`stop` asks the controller to stop at its next boundary and closes the owned
station after the controller stops; a later resume creates or reconnects the
station as needed. `stop --force` reconciles unresolved work and terminates
only a process group or tmux station whose persisted ownership can be
validated. `cancel` durably cancels the mission and its unfinished tasks.

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

## Local state and config

Mission state defaults to
`${XDG_STATE_HOME:-~/.local/state}/llm-tools/llm-bridge`. Set
`LLM_BRIDGE_STATE_DIR` to use another directory. Bridge stores its SQLite
database, private runtime files, and logs there. The shared `config.toml` has
no `[bridge]` section; choose providers, routes, and execution mode on the
mission creation/start commands. The selected shared tool configuration is
captured for the mission so later changes do not silently alter its routing.

Bridge tmux sockets use a short private runtime path under
`${XDG_RUNTIME_DIR}/llm-bridge` when available, or `/tmp/lb-UID`, with a
mission-ID hash in the socket name. Controller runtime files remain under the
private Bridge state directory. Provider output is rotated at 4 MiB per file
with three rotations by default. Bridge retains the latest 20 run directories;
when older directories are pruned it records log size and a SHA-256 digest in
the mission database before deleting the files. Adjust these limits with the
mission creation flags above.

See `llm-bridge --help` and the command-specific `--help` output for all
available CLI operations.

Read a local textual artifact with `llm-bridge view --file notes.md`. Use
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
