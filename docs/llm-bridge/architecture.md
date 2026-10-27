# LLM Bridge architecture and contracts

Planning baseline: 2026-10-25. Requirement: user-owned
`docs/features/llm-bridge/prompt.md`, preserved unchanged. This is a contract,
not a claim of completed implementation or provider compatibility.

## Reuse and ownership

Python 3.11+, standard library, setuptools entry points, pytest, branch coverage
>=85%. Actual repository inspection finds:

- `ralph_robin.main` owns persistent iteration, failure streaks, rotation,
  `select_provider`/`select_route`, completed counts, inhibition and suspend.
- `scheduler_config_for`/`run_scheduler_inline` carry route/model settings into
  scheduler readiness, retries, `submit_once` and runtime-block classification.
- `scheduler.provider_default_argv` distinguishes attached/headless launches.
  Root's installed-CLI investigation found drift; use its capability ledger.
- `common.run_pty_capture` is the autonomous PTY/idle/prompt guard.
- `routes.usage_snapshot_and_decision_for_route`, capacity dataclasses and
  usage sampler provide structured capacity; never parse dashboard output.

Bridge owns missions/tasks, verification, command dispatch, presentation,
session/process ownership and durable events. Existing Ralph/scheduler/usage
remain authoritative in their domains. No second provider-selection or retry
engine. Default no-hook behavior of existing tools remains compatible.

## Terminal architecture

Choose private tmux socket/session, real provider PTY pane, Bridge command pane
and compact status. The local version is tmux 3.6. Discover required capabilities;
document the tested minimum. Headless management works without tmux, interactive
attach reports an actionable missing dependency.

| Choice | Assessment |
| --- | --- |
| Dedicated PTY with embedded native viewport | Requires VT emulation, cursor/alternate screen, scrollback, mouse coordinate translation, paste and focus state. Raw curses output relay is insufficient. |
| Native tmux panes | tmux owns these terminal responsibilities, attach/detach and resize. Adds a system executable but no Python framework. Selected for fidelity and reduced risk. |

Existing `scheduler.run_tmux` cannot serve unchanged: it sends shell commands
to existing windows and polls screen capture. Add a narrow owned-pane transport
hook while retaining scheduler exit classification and runtime blocks. Use argv
interfaces or private scripts with shlex.quote, never output interpolation.

At 80x24 prioritize the native viewport; compact status/command area. Orders,
Stations, Systems, Log, Sessions and Palette can use a separate full control
view. Private-server prefix default Ctrl-], configurable, with literal
send-prefix. tmux handles native pane mouse/resize/paste; Bridge keyboard and
curses mouse controls share command handlers. Native selection may require
Shift or tmux copy mode. Do not write a second raw mouse relay.

Autonomous mode displays real headless CLI output as an activity feed. Native
supervised mode runs the actual interactive CLI, including menus and shortcuts;
it normally remains alive after a model turn and needs an explicit operator
checkpoint. Neither mode impersonates the other. Viewed and executing stations
are separate. Opening an interactive writer first pauses autonomous scheduling
and waits for the current iteration; no implicit concurrent worktree writers.

Actual CLI drift discovered by root: Kilo native uses default CLI --prompt,
OpenCode native needs --prompt, mmx 1.0.16 is a resource/text-chat CLI without the
assumed coding-agent run command. Do not claim an mmx native engineering station.
See root-owned provider-capabilities.md for versions and supported resume flags.

## Persistence and concurrency

Use stdlib sqlite3. The user's explicit SQLite/controller authorization overrides
the old no-database/no-additional-server default for Bridge only. Atomic JSON
files would require a journal and multi-file commit/recovery implementation for
task+command+event consistency. SQLite WAL, synchronous FULL, foreign keys,
bounded busy timeout, BEGIN IMMEDIATE and transactional migrations are smaller.

Database: `${XDG_STATE_HOME:-~/.local/state}/llm-tools/llm-bridge/bridge.sqlite3`,
override `LLM_BRIDGE_STATE_DIR`. Missions belong in durable state, not cache.
Directories 0700, DB/WAL/SHM/logs 0600. No credentials/environment dumps. Reject
unknown newer schemas; `PRAGMA user_version` migrations roll back on error.
No network filesystem or external database claim.

Minimum entities: missions (objective verbatim, cwd/repository identity, frozen
non-secret config, lifecycle); tasks (verbatim original prompt, state, priority,
order, revision, checkpoint, provenance); dependencies; immutable amendments;
criteria; evidence (revision, result, verifier/artifact digest, private log ref);
operations (intent/phase/outcome/uncertainty, route/model, process identity);
stations (configured versus observed model/effort, native session id,
restoration state); commands (unique request id, validated payload, result);
events (monotonic id, schema, time, mission/task/operation ids, bounded payload).

State change and event commit in the same transaction. UI/JSON are projections,
not alternate sources of truth. Page histories; bound in-memory snapshots and
rotate raw output separately. Evidence files use fsync+rename before DB reference.
No untrusted provider/repository text enters operator command dispatch.

One controller per mission holds lifetime kernel flock. Canonical worktree lock
prevents competing mutating missions. Record boot/process-start identity and
owned private tmux pane/session identifiers; PID alone never authorizes a signal.
Concurrent clients enqueue commands transactionally, including when controller
is absent. Distinguish queued/applied/rejected/failed; enqueue is not execution.
Multiple observers are safe. Commands require no new socket server: bounded
idle wait <=1s suffices. No global daemon/network listener.

## State and interruption contracts

Mission: CREATED -> RUNNING <-> PAUSED; RUNNING/PAUSED -> STOPPING -> STOPPED;
STOPPED -> RUNNING after reconciliation. All verified tasks -> COMPLETED.
Explicit cancellation -> CANCELLED. Controller errors -> FAILED, explicit resume
only. Terminal mission actions remain inspectable.

| Task state | Allowed next states |
| --- | --- |
| QUEUED | READY, PAUSED, BLOCKED, CANCELLED |
| READY | RUNNING, QUEUED, PAUSED, BLOCKED, CANCELLED |
| RUNNING | VERIFYING, READY (checkpoint continuation), PAUSED, BLOCKED, FAILED, CANCELLED |
| VERIFYING | COMPLETED, READY (bounded failed-verifier retry), BLOCKED, FAILED, CANCELLED |
| PAUSED | QUEUED/READY after dependencies, CANCELLED |
| BLOCKED | QUEUED/READY after explicit reconciliation, FAILED, CANCELLED |
| FAILED | QUEUED through explicit retry, CANCELLED |
| COMPLETED/CANCELLED | terminal; further work is a follow-up task |

Only completed prerequisites unlock READY. Reject dependency cycles including
concurrent changes transactionally. Choice: priority descending, explicit order
ascending, creation id ascending. Agent proposals need operator acceptance.
Amendments preserve original text, increment revision and conservatively
invalidate outdated verification evidence.

Commit operation PREPARED before spawn, then RUNNING with owned identity, then
observed exit and verification. Spawn-before-identity crash window stays
UNCERTAIN. Recovery marks unresolved operations UNCERTAIN and task BLOCKED.
Process disappearance, status files and provider zero exit never prove completion.
No silent replay of potentially non-idempotent work: explicit reconciliation,
verification or authorized retry creates a linked new operation. Guarantee
durable intent and one active controller, not exactly-once external effects.

## Minimal execution hooks

Expose optional hooks around the existing Ralph loop (or extract the loop into
a reusable function with unchanged CLI defaults):

1. before_iteration gets next task prompt/context or PAUSE/STOP, applying queued
   steering only at this safe boundary.
2. selection receives existing structured route/model/usage decisions.
3. before_launch commits operation intent before provider spawn; hook failure
   prevents launch.
4. after_iteration receives raw scheduler outcome/log/session references;
   checkpoints and verifies, then returns continue/next task/pause.
5. waiting/suspend publishes existing outcomes and checks stop during long waits.

Names can follow local conventions; semantics are required. Keep route selection,
failure budgets, readiness, retries, inhibition, runtime blocks and suspend in
Ralph/scheduler. Per-mission Ralph state/log paths avoid overwriting global state.
Do not launch ralph-robin then parse its formatted stdout to discover state.

## Verification, steering and sessions

Explicit criteria: command argv with timeout/cwd/private logs, artifact checks,
or operator approval. No shell unless explicitly selected. Evidence is tied to
task revision. Exit zero merely triggers VERIFYING; no criteria means awaiting
operator verification, never implicit completion. task verify --approve records
operator evidence. Show criteria counts, not invented completion percentages.

Persist typed new-task/amend/replace/review/verify/pause-after-iteration/
stop-after-task/handoff commands; apply at safe boundaries. Handoff includes
bounded objective/revision/checkpoint/evidence refs and fresh Ralph runtime
context. Native conversation state never transfers across providers. A bounded
no-progress budget uses verified progress/failure signatures conservatively;
long builds within their timeout are not stalls.

Pause prevents new iterations, retaining processes. Detach removes UI only.
Graceful stop blocks launches, waits checkpoint, flushes state and terminates
owned stations. Timeout remains STOPPING and requires explicit force. Force
persists uncertainty before terminating owned process groups. Cancel durably
abandons mission. These operations never call machine suspend.

Resume acquires ownership, reconciles incomplete operations, refreshes capacity
and reconstructs task/config/stations independently of old tmux/PTY existence.
Restoration: NOT_STARTED, RESTORING, RESTORED_NATIVE, RECONSTRUCTED, UNAVAILABLE,
FAILED. Native resume requires captured validated provider-specific session id
and supported tested argv, never global --last. If identity cannot be verified,
use RECONSTRUCTED. Configured versus observed model/effort remain separate;
unknown stays unknown. Root owns actual provider capability evidence.

## CLI contract

Entry point `llm_tools.bridge:main`, executable llm-bridge. Common --mission ID,
--json and state override. JSON has schema_version and structured errors;
usage errors exit 2, successful finite commands 0.

```
llm-bridge --prompt-file objective.md [--cwd PATH] [--headless]
llm-bridge mission create --file objective.md [--name NAME] [--cwd PATH]
llm-bridge mission list [--json]
llm-bridge status --mission ID [--json]
llm-bridge start|pause|resume|stop|cancel|attach|detach --mission ID
llm-bridge stop --mission ID --force
llm-bridge task add --mission ID --file task.md [--priority N]
llm-bridge task list|show --mission ID [--task ID] [--json]
llm-bridge task amend --mission ID --task ID --file amendment.md
llm-bridge task priority|reorder|dependencies --mission ID --task ID ...
llm-bridge task pause|resume|cancel|retry|verify --mission ID --task ID
llm-bridge task verify --mission ID --task ID --approve
llm-bridge events --mission ID [--after ID] [--limit N] [--json]
llm-bridge doctor [--mission ID] [--json]
```

Palette/mouse/keyboard use same handlers. Multiline entry via private editor file
preserves content. Status includes desired/observed modes, controller ownership,
executing/viewed station, pending commands, evidence counts and last progress.
Slow capacity refresh stays outside input/render path. Refresh only changed
state <=4Hz; memory/history bounded. Stress target: input dispatch <250ms under
1MiB/s fake output and 10k tasks/events; measure CPU/RSS rather than inventing
results. Native feasibility must pass before the full UI.
