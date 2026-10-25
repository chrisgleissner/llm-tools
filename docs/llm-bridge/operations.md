# LLM Bridge operations

LLM Bridge stores missions and task history locally. It connects task order,
provider operations, and verification evidence so an operator can inspect what
has happened before approving completion or retrying uncertain work.

## Install and create a mission

Install `llm-tools` as usual. The console station needs `tmux`; `--headless`
can run a mission without it. The existing provider CLIs and credentials are
used by the scheduler. Bridge adds no provider SDK or network service.

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

List missions with `llm-bridge mission list`, and inspect one with:

```bash
llm-bridge status --mission MISSION_ID
llm-bridge events --mission MISSION_ID --after 0 --limit 100
llm-bridge doctor --mission MISSION_ID
```

`status` reports the mission, tasks and their states, recent operations,
pending commands, and recent events. Add `--json` to these commands for
machine-readable output. `doctor` reports the state directory, SQLite
integrity, and tmux availability.

## Task ordering and verification

Add a task with a verbatim file or standard input. Dependencies refer to task
IDs and prevent a task from becoming ready until each prerequisite is
completed. Higher priority runs first, then lower explicit order, then older
task ID:

```bash
llm-bridge task add --mission MISSION_ID --file follow-up.md \
  --priority 5 --depends-on 1 --acceptance "tests pass"
llm-bridge task list --mission MISSION_ID
llm-bridge task show --mission MISSION_ID --task TASK_ID
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

It offers seven views: Bridge, Orders, Stations, Systems, Log, Sessions, and
Palette. Keyboard and mouse operate those views:

| Input | Action |
| --- | --- |
| Tab / Shift-Tab, Left / Right, F1–F7 | Change view |
| Up / Down | Select a row or palette command when the command line is empty |
| Click a view label | Change view |
| Click a palette row, then Enter | Select and run that command |
| Wheel up / down | Move the selection |
| Ctrl-E | Open `$VISUAL` or `$EDITOR` to add a multiline task |
| Ctrl-R | Open the editor to amend the selected order |
| Enter | Run the command in the input line |
| Ctrl-P / Ctrl-N | Previous / next command history item |
| Ctrl-U / Esc | Clear the input line |
| Ctrl-D / Ctrl-C | Exit when the input line is empty |

The editor is launched as an argv (not through a shell) on a private temporary
file. Saving non-empty text submits the task; cancelling the editor or saving
an empty file submits nothing. The UI accepts printable command text without
using letters as view shortcuts. The private tmux station starts at 80 columns
by 24 rows or larger.

## Attach, pause, stop, and resume

Attach to the private tmux station with `llm-bridge attach --mission ID` and
remove attached clients with `llm-bridge detach --mission ID`. Detaching only
closes the terminal view; it does not pause the mission or stop a provider.
Headless missions have no station to attach to.

`pause` prevents a new provider iteration from starting. If a provider is
already working, it runs until that invocation returns, then the controller
waits for `resume`. Pause does not suspend the machine. `resume` continues an
active controller or starts one if needed. `stop` asks the controller to stop
at its next boundary; `stop --force` reconciles unresolved work and terminates
only a process group or tmux station whose persisted ownership can be
validated. `cancel` durably cancels the mission and its unfinished tasks.

After a controller restart, Bridge reconciles any operation that was in
progress. It may mark the operation uncertain and the task blocked for review;
it does not treat a vanished process or a provider's zero exit as proof that
the task is complete, and it does not silently replay uncertain work. Inspect
`status` and `events`, then use explicit verification, task retry, or mission
resume after resolving the uncertainty.

The provider pane in native mode is a real provider terminal UI while that
process is running. Bridge does not claim to restore an exact provider
conversation after restart or switch providers. Follow-up work is reconstructed
from the durable objective, task revision, checkpoints, operation records, and
verification evidence; provider conversation context may be unavailable.

## Local state and config

Mission state defaults to
`${XDG_STATE_HOME:-~/.local/state}/llm-tools/llm-bridge`. Set
`LLM_BRIDGE_STATE_DIR` to use another directory. Bridge stores its SQLite
database, private runtime files, and logs there. The shared `config.toml` has
no `[bridge]` section; choose providers, routes, and execution mode on the
mission creation/start commands. The selected shared tool configuration is
captured for the mission so later changes do not silently alter its routing.

See `llm-bridge --help` and the command-specific `--help` output for all
available CLI operations.
