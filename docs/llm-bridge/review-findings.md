# Independent review findings

Review owner: Luna reviewer. Findings are independent of the implementation
owner. Open items block their corresponding milestone at the listed severity.

## Gate 1 — Persistence and mission lifecycle

Reviewed the frozen persistence/runtime checkpoint in the working tree based
on HEAD
`a8e6bf69a4cee875a1cf2a0a713c1e749045b290` on 2026-10-25. The focused store,
process-level and command-atomicity suites passed. I independently reran
`python -m pytest tests/test_bridge_*.py tests/test_terminal_backend.py
tests/test_scheduler_owned_pane.py
tests/test_additional_paths.py::test_scheduler_system_and_tmux_helpers
tests/test_coverage_boost.py::test_provider_default_argv_kilo_and_opencode_cwd_handling -q`:
94 passed. The implementor's verifier evidence and process-identity changes
are included in this frozen checkpoint.
After that 94-test run, Ralph/Bridge pre-launch handling changed and its
targeted hook command passed 3 tests. A separate task-cancel interleaving still
escapes as a controller error; no full suite was rerun after that change.

### P1 — High: task completion can bypass verification — CLOSED

- **Location:** `llm_tools/bridge_store.py` (`verify_task`, `set_task_status`).
- **Initial reproduction:** Before the frozen revision,
  `set_task_status(task_id, "COMPLETED")` accepted a task in VERIFYING with an
  empty evidence list.
- **Fix:** The generic task transition map no longer allows VERIFYING to
  COMPLETED. Completion goes through `verify_task`.
- **Verification:** Replayed the setter transition after the fix; it raises
  `TransitionError`. Focused store tests pass.

### P2 — High: an amendment added during verification can be skipped — CLOSED

- **Location:** `llm_tools/bridge_store.py` (`amend_task`, `verify_task`).
- **Initial reproduction:** Finish an operation with exit code 0 so the task
  becomes VERIFYING; call `amend_task(prompt="new objective")`; then call
  `verify_task(..., evidence="old run passed")`. The task became COMPLETED
  with the new prompt still pending.
- **Impact:** Steering is recorded but ignored, while stale work is accepted as
  satisfying the newer task revision.
- **Fix:** An amendment arriving during VERIFYING is applied immediately,
  clears prior verification evidence and returns the task to READY. Evidence
  records the task revision, and verification rejects any still-pending
  amendment.
- **Verification:** The new
  `test_amendment_after_exit_invalidates_verification_and_requeues_task`
  covers this sequence; focused store tests pass.

### P3 — High: a mission can complete with unfinished tasks — CLOSED

- **Location:** `llm_tools/bridge_store.py` (`set_mission_status`).
- **Initial reproduction:** Before the frozen revision, RUNNING to COMPLETED
  succeeded with a QUEUED task.
- **Fix:** The store now requires at least one task and requires every mission
  task to be COMPLETED before accepting mission completion.
- **Verification:** Replayed the transition against the frozen store; it raises
  `TransitionError` while a task is queued.

### P4 — High: sibling working directories in one Git worktree can run at once — CLOSED

- **Location:** `llm_tools/bridge_store.py` (`create_mission`, worktree
  exclusion).
- **Initial reproduction:** In a temporary Git repository with sibling directories
  `repo/a` and `repo/b`, create one mission for each directory and set both to
  RUNNING. Both are accepted. The current overlap check catches ancestor and
  descendant paths, but `repo_identity` is still the exact `cwd`.
- **Impact:** Two missions can mutate one Git worktree concurrently, despite
  the single writer contract.
- **Fix:** `repository_identity()` resolves `git rev-parse --show-toplevel`,
  migration v3 backfills existing mission identities, and the active-worktree
  index is keyed by that identity.
- **Verification:**
  `test_sibling_directories_in_one_git_worktree_are_exclusive` passes.

### P5 — High: dependency changes can let active work complete out of order — CLOSED

- **Location:** `llm_tools/bridge_store.py` (`add_dependency`,
  `finish_operation`, `verify_task`).
- **Initial reproduction:** The earlier RUNNING case was fixed, but a
  VERIFYING task could still gain an incomplete prerequisite and then complete;
  a PAUSED task could be returned as READY despite an incomplete prerequisite.
- **Impact:** Dependency ordering can be bypassed by changing dependencies
  during execution or verification.
- **Fix:** Dependency edits reject RUNNING and VERIFYING tasks. One shared
  prerequisite predicate now gates operation start, verification, and READY
  state; `ready_tasks()` filters unresolved dependencies, including when rows
  are already marked READY. PAUSED tasks can return to QUEUED but cannot become
  READY until prerequisites complete.
- **Verification:**
  `test_dependency_invariant_covers_verifying_paused_ready_and_completion`
  checks the VERIFYING edit and verification paths, PAUSED-to-READY rejection,
  and ready-list filtering. The full focused store/concurrency/dispatch suite
  passes (28 passed).

### P6 — Medium: a failed COMMIT leaves the connection in a transaction — CLOSED

- **Location:** `llm_tools/bridge_store.py` (`_transaction`).
- **Initial reproduction:** A deferred foreign key violation raised
  `sqlite3.IntegrityError` during COMMIT; `db.in_transaction` remains true and
  the next `add_task()` raises `cannot start a transaction within a
  transaction`.
- **Impact:** A handled storage error poisons the live store and prevents
  graceful shutdown or recovery writes.
- **Fix:** COMMIT now runs inside the guarded transaction body. If it fails,
  the store rolls back when SQLite reports an active transaction.
- **Verification:** The new
  `test_commit_failure_rolls_back_and_leaves_connection_reusable` injects a
  deferred constraint failure and confirms a later write succeeds.

### P7 — Medium: WAL and shared-memory file permissions — CLOSED

- **Location:** `llm_tools/bridge_store.py` (`_secure_sqlite_files`).
- **Initial reproduction:** Before the frozen revision, SQLite created WAL and
  SHM sidecars as 0644 while the main database was 0600.
- **Fix:** `_secure_sqlite_files()` applies 0600 to the database and sidecars
  after schema setup and transactions.
- **Verification:** Rechecked after task writes; database, WAL and SHM were all
  0600. The containing directory remains 0700.

### P8 — Medium: process-level crash and concurrency evidence — CLOSED

- **Location:** `tests/test_bridge_concurrency.py`.
- **Evidence:** Four real child writers retain all 160 tasks, order values and
  events; `os._exit` inside an open SQLite transaction reopens the prior state
  with no partial event; killing the lock-owning child releases the kernel
  flock; PREPARED and RUNNING operations survive child death, reconcile to
  UNCERTAIN/BLOCKED and are not replayed after a second reopen.
- **Verification:** `python -m pytest tests/test_bridge_concurrency.py -q`:
  5 passed. This closes these store-level boundaries, not later end-to-end
  stop/resume and reconstruction review.

### P9 — High: opening the database depends on every historical working directory — CLOSED

- **Location:** `llm_tools/bridge_store.py` (`_initialize`).
- **Initial reproduction:** Opening an already-v3 store after deleting one
  historical mission cwd raised `FileNotFoundError` before serving any mission;
  every open also recomputed each stored Git identity.
- **Fix:** Repository identity backfill only runs when the schema version is
  upgraded. Migration preserves the old identity when a historical cwd no
  longer exists; current-schema opens trust the persisted value.
- **Verification:** `test_current_schema_open_preserves_identity_for_removed_worktree`
  and `test_migration_preserves_identity_when_historical_cwd_is_gone` cover
  current open and migration behavior. I independently reopened a database
  after deleting its mission cwd; reading the mission succeeded.

## Gate 2 — Native terminal transport

Reviewed `llm_tools/terminal.py`, `tests/test_terminal_backend.py` and the
outer-PTY test on 2026-10-25. The focused backend/attach/TUI suite passed (16
passed); the attached PTY test then passed five additional independent runs.
The broader terminal, TUI and owned-pane gate passed 26 tests on the latest
independent run.

### T1 — High: clicking the Bridge pane does not focus it — CLOSED

- **Location:** private tmux configuration in `llm_tools/terminal.py` and
  `tests/test_terminal_attached.py`.
- **Initial reproduction:** In the outer-PTY test, click the Bridge pane at row 37 and
  then type `controls`. The Bridge fake receives the mouse bytes, but keyboard
  bytes go to the provider pane; the test times out waiting for `controls.bin`.
  This was reproduced independently from the current tree.
- **Impact:** Mouse users cannot direct keyboard input to the selected Bridge
  control pane; the provider pane keeps receiving input.
- **Fix:** Private tmux bindings select the clicked pane and forward the event.
- **Verification:** The updated outer-PTY test confirms the Bridge pane receives
  the mouse event and subsequent keyboard input while provider input remains
  isolated; the focused backend and attached suite passes (6 passed).

### T2 — Medium: the private tmux prefix consumes native Ctrl-B

- **Location:** private tmux configuration in `llm_tools/terminal.py`.
- **Initial reproduction:** The private server defaulted to C-b and the
  attached test detached with `C-b d`.
- **Impact:** Native provider applications cannot receive Ctrl-B shortcuts,
  contrary to the selected Ctrl-] prefix contract.
- **Fix:** The private server now uses C-] and binds literal send-prefix. A
  manual outer-PTY probe confirmed `prefix2=None` and Ctrl-B reaches the fake
  provider.
- **Verification:** The attached outer-PTY test confirms `prefix2` is unset,
  sends Ctrl-B and confirms the provider receives it, then checks Ctrl-]
  literal-prefix forwarding and the Ctrl-] detach binding after synchronizing
  tmux client and pane resize geometry. The 16-test backend/attach/TUI suite
  passed, followed by five consecutive independent attach-test passes.

### T3 — Medium: focus events are disabled — CLOSED

- **Location:** private tmux configuration in `llm_tools/terminal.py`.
- **Initial reproduction:** `focus-events` was off.
- **Impact:** Provider TUIs requesting focus reports do not receive focus-in or
  focus-out events when the operator switches panes.
- **Fix and verification:** Focus-event forwarding is enabled for the private
  server. The outer-PTY fake requests focus reports; switching panes delivers
  focus-out/in sequences to provider and Bridge panes.

### T4 — Medium: `is_alive()` reports a dead provider pane as alive — CLOSED

- **Location:** `llm_tools/terminal.py` (`is_alive`).
- **Initial reproduction:** A provider that printed and exited left a retained
  tmux session, and the old `is_alive()` returned `True`.
- **Fix:** Session existence and pane liveness are exposed separately; provider
  liveness queries the structured dead-pane state.
- **Verification:** The rapid-exit test confirms the provider pane is dead while
  the Bridge pane and server remain available for inspection.

<!--
- **Old reproduction detail:** Start a provider that prints and exits. After 150 ms,
  `is_alive(handle)` returns `True`; tmux reports `pane_dead=1:0` because
  `remain-on-exit` retains the pane.
- **Impact:** Callers can confuse a retained tmux session with a running
  provider and report stale station activity.
- **Required fix:** Expose session existence separately from provider process
  liveness, querying structured pane/process state for the latter.
- **Verification:** Test a long-running provider and rapid successful/failed
  exits; liveness results must differ while the retained pane remains
  inspectable.
-->

### T5 — Medium: the resize test asserts the old PTY size — CLOSED

- **Location:** `tests/test_terminal_backend.py`.
- **Initial reproduction:** The old test requested 120x40, then immediately sends input and
  asserts the child reports `23x100`, the original 100x30 PTY dimensions in
  rows-by-columns order. A synchronized child read after resize reports
  33x120. Its later pane-size assertion checks tmux geometry, not the provider
  PTY.
- **Impact:** The current test can pass without proving the child received a
  resize or SIGWINCH.
- **Fix and verification:** The outer-PTY fake records SIGWINCH and dimensions;
  resizing the attached client to 80x24 produces 80x17 in the provider PTY
  after reserving six rows for the Bridge pane.

### T6 — Medium: CI can silently skip the PTY tests — CLOSED

- **Location:** terminal test skip markers and `.github/workflows/test.yml`.
- **Initial reproduction:** Both terminal test modules skip when tmux is missing; the
  current workflow does not install tmux. A runner without tmux can report both
  tests skipped while the job passes.
- **Impact:** CI can appear green without exercising native terminal transport.
- **Fix:** CI explicitly installs tmux and checks `command -v tmux` before
  running pytest. The local skip remains useful on developer machines.
- **Verification:** The current local run executed all six focused backend and
  attach tests; no CI run has yet been reviewed.

## Gate 3 — Operator command dispatch

Reviewed `llm_tools/bridge.py` command application and
`tests/test_bridge_dispatch_atomicity.py` on 2026-10-25.

### O1 — High: task effect and command acknowledgement commit separately — CLOSED

- **Location:** `llm_tools/bridge.py` (`_apply_operator_command`) and
  `llm_tools/bridge_store.py` (`add_task`, `finish_command`).
- **Reproduction:**
  `python -m pytest tests/test_bridge_dispatch_atomicity.py -q` injects a
  `KeyboardInterrupt` when acknowledgement begins after `task.add`. The task
  row is already committed while the command remains QUEUED; reopening the
  database confirms the split state.
- **Impact:** Reapplying a queued non-idempotent command can create duplicate
  tasks, and the event history cannot establish whether the operator command
  took effect.
- **Fix:** `_apply_operator_command()` wraps each effect and
  `finish_command()` in the same outer SQLite transaction. Store methods use
  nested savepoints, so an interruption rolls back the task/event while leaving
  the already durable QUEUED intent available for inspection. No startup path
  silently replays it.
- **Verification:** `test_command_interruption_cannot_leave_applied_task_without_acknowledgement`
  injects `KeyboardInterrupt` before acknowledgement, then checks both the
  live connection and a reopened database: no task or `task-added` event is
  visible, and exactly one QUEUED command remains. The 28-test focused suite
  passes.

### O2 — High: active-task pause/cancel is overwritten by its later exit — CLOSED

- **Location:** `llm_tools/bridge.py` (`task.cancel` command) and
  `llm_tools/bridge_store.py` (`finish_operation`).
- **Initial reproduction:** Starting an operation, applying task pause/cancel,
  then finishing with exit 0 rewrote the requested state to VERIFYING.
- **Fix:** `finish_operation()` now retains PAUSED/CANCELLED task state and
  mission PAUSED at the operation boundary. An active task pause reports
  `work_status=DEFERRED` and explains that it takes effect when the provider
  returns.
- **Verification:** `test_successful_child_exit_preserves_operator_task_state`
  covers PAUSED and CANCELLED task states;
  `test_successful_child_exit_observes_mission_pause` covers mission pause.
  The focused 28-test persistence/concurrency/dispatch suite passes.

### O5 — High: active task cancellation hides the still-running provider — CLOSED

- **Location:** `llm_tools/bridge.py` (`task.cancel`) and the owned provider
  operation.
- **Initial reproduction:** `task.cancel` left a RUNNING operation without a
  deferred-work result even though it did not signal the provider.
- **Fix:** Active cancellation now returns `work_status=DEFERRED`, the active
  operation ID, and a clear boundary description. The operation remains
  tracked; successful or failed provider exit does not overwrite CANCELLED.
- **Verification:** `test_task_cancel_reports_the_active_provider_operation_as_deferred`
  checks the command result and RUNNING operation; store regression
  `test_successful_child_exit_preserves_operator_task_state` checks exit does
  not overwrite CANCELLED. The final focused integration command passed.

## Gate 4 — Ralph hook integration

### O3 — High: a stop or task cancel racing pre-launch is misreported as controller failure — CLOSED

- **Location:** `llm_tools/ralph_robin.py` (`main`) and
  `llm_tools/bridge_runtime.py` (`BridgeMissionHooks.before_launch`).
- **Fix:** Ralph skips scheduler launch when the hook rejects the boundary.
  `begin_operation()` validates mission/task state in its transaction, and
  `before_launch()` treats a rejected intent as normal when either the mission
  left RUNNING or the task became PAUSED/CANCELLED; unrelated transition errors
  still propagate.
- **Verification:** `test_before_launch_treats_concurrent_mission_stop_as_rejected_launch`
  covers the mission-stop race. I independently injected task cancellation
  between selection and `begin_operation()`; the hook returned `False`, left
  the mission RUNNING/task CANCELLED, and created zero operations. The task
  cancel interleaving has no dedicated checked-in regression yet.

### O4 — High: stop requests are not polled during Ralph/scheduler waits — CLOSED

- **Location:** `llm_tools/ralph_robin.py` (`suspend_until_available`,
  `suspend_machine_until`) and `llm_tools/scheduler.py`
  (`wait_until_usable`).
- **Fix:** Bridge supplies an interrupt callback to Ralph awake waits and
  scheduler readiness/retry waits. Bridge waits remain awake so operator
  requests stay responsive; no-hook Ralph/scheduler calls retain their default
  behavior.
- **Verification:** The Ralph wall-clock wait, Bridge cross-provider wait,
  scheduler readiness wait, and detached long-capacity-wait tests cover
  prompt interruption without launching a provider. All passed in the final
  35-test integrated review command.

## Gate 5 — Controller recovery and restoration

### O6 — High: a stale ready file can falsely confirm a new controller — CLOSED

- **Location:** `llm_tools/bridge_runtime.py` (`launch_controller`,
  `controller-ready.json`).
- **Reproduction:** Leave a ready file from an earlier run containing
  `{"pid": 123, "status": "RUNNING"}`. Stub the newly spawned controller as
  PID 999 without letting it write readiness. `launch_controller()` returns 0
  and prints that the mission is running while the file still identifies PID
  123.
- **Impact:** After controller loss, a start/resume can report success before
  the new owner is ready; interactive mode may attach to a stale or missing
  terminal session.
- **Fix:** `launch_controller()` unlinks readiness before spawn and requires
  both the spawned PID and a fresh startup token.
- **Verification:** `test_detached_launch_never_accepts_a_stale_ready_file`
  seeds stale readiness and stubs a child that never writes a fresh record; the
  launch fails instead of accepting it. The 94-test execution/restoration
  suite passed.

### O7 — High: graceful stop can report STOPPED before terminal cleanup succeeds — CLOSED

- **Location:** `llm_tools/bridge_runtime.py` (`run_controller` shutdown) and
  `llm_tools/bridge.py` (`stop` command).
- **Evidence:** The normal path now kills the private server in
  `run_controller()` and the real-tmux test confirms eventual station cleanup.
  However, `BridgeMissionHooks.before_iteration()` writes STOPPED before the
  controller's `finally` calls `backend.stop(handle)`. `TmuxTerminalBackend.stop()`
  treats a non-responsive/unowned probe or failed/timed-out `kill-server` as a
  no-op, unlinks the socket/config anyway, and does not verify server exit.
- **Reproduction:** I started a real private tmux server with two 30-second
  pane processes, injected a nonzero `kill-server` result, then called
  `backend.stop()`. It returned normally, removed socket and config, and both
  pane PIDs remained alive.
- **Impact:** A failed terminal teardown can leave provider/Bridge panes alive
  while the durable mission already says STOPPED; a start/stop caller has no
  reliable state indicating cleanup is incomplete.
- **Required fix:** Keep STOPPING durable through cleanup. Make stop report
  failure if the owned server cannot be proven gone, and do not remove its
  ownership files or mark STOPPED until server/session exit is verified.
- **Verification:** `test_graceful_stop_terminates_tmux_station_and_bridge_ui`
  proves the success path, but it observes state only after cleanup and does
  not inject tmux kill failure. Add an ordering/failure regression and, if
  feasible, assert attached-client return through the CLI stop path.

### O8 — High: recovery can accept an unverified terminal teardown — CLOSED

- **Location:** `llm_tools/bridge_runtime.py` (`run_controller` recovery and
  `_open_existing`).
- **Fix:** Recovery reconciles the operation to UNCERTAIN/BLOCKED, stops the
  prior private station or exact persisted headless process group, and the new
  real-tmux regression confirms the surviving provider is terminated before
  the replacement controller is ready. Work is not replayed.
- **Remaining failure path:** Recovery treats a non-`None` `prior_handle` as
  sufficient even though `TmuxTerminalBackend.stop()` returned normally in the
  injected kill-failure reproduction while both pane processes remained alive.
  That bypasses the refusal for an unidentified RUNNING operation and can let a
  replacement controller become ready while an old provider pane remains alive.
- **Required fix:** Verify the owned tmux server and provider pane are gone
  before proceeding or writing readiness; if teardown cannot be proved, keep
  the mission in explicit recovery and do not start replacement work.
- **Verification:** `test_resume_reconciles_native_mutator_and_stops_old_station`
  passes the ordinary success path. Add a kill-failure/unidentified-process
  regression proving resume refuses readiness and never launches another task.

### O9 — High: first native session capture can bind an unrelated Codex conversation — CLOSED

- **Location:** `llm_tools/bridge_runtime.py` (`_native_session_argv_transform`,
  `_record_session_outcome`) and `llm_tools/bridge_sessions.py`.
- **Initial reproduction:** The earlier Codex path selected a new same-cwd
  transcript by modification time and could persist an unrelated conversation
  as `NATIVE_CREATED`.
- **Fix:** Codex no longer auto-binds a new native session because Bridge cannot
  choose its ID. Its old saved IDs without `owned_by_bridge: true` are ignored,
  and Codex continues from reconstructed task context. Claude and Copilot use a
  Bridge-generated UUID passed through their advertised `--session-id` option;
  Bridge accepts only the provider's structured metadata for that exact ID and
  matching cwd, then persists that ID as Bridge-owned. Resume uses the exact
  persisted ID and is rejected on cwd or capability mismatch. There is no
  timestamp-based candidate discovery.
- **Verification:** Independently ran
  `python -m pytest tests/test_bridge_sessions.py tests/test_bridge_runtime.py
  tests/test_bridge_store.py tests/test_bridge_wait_interrupt.py -q`:
  67 passed. The tests cover the Codex unrelated-session fallback, exact
  Bridge-generated Claude/Copilot IDs, reopening SQLite and a new hook instance,
  exact-ID resume, and cwd/capability mismatch. These are hermetic fixtures;
  they do not constitute a live provider restoration test or an OS-level
  controller restart test. Installed-provider native restore remains
  **NOT VERIFIED**, and unsupported providers remain reconstructed/unavailable.

### O10 — High: force stop leaves an independent verifier running — CLOSED

- **Location:** `llm_tools/bridge_runtime.py` (`_run_bounded_verifier`,
  `run_configured_verifier`) and `llm_tools/bridge.py` (`stop --force`).
- **Reproduction:** I started a headless mission with a successful fake
  provider and a configured verifier that wrote its PID then slept. Once the
  provider operation was EXITED and verification was running,
  `llm-bridge stop --force` returned 0 and marked the mission stopped, but the
  verifier PID remained alive. I terminated that process group during cleanup.
- **Cause:** The verifier uses `start_new_session=True`, while force stop only
  reconciles active provider operations and kills the controller process
  group. The verifier has no persisted PID/start-token ownership record, and
  its provider operation is already EXITED, so force stop cannot identify its
  separate process group.
- **Impact:** Verifier work can keep running after force stop reports success
  and may continue reading or mutating the mission worktree after cancellation.
- **Required fix:** Track and validate verifier process ownership during its
  lifetime, terminate its exact process group on force stop, and wait for that
  group to exit before marking the mission STOPPED. Keep timeout descendant
  cleanup intact.
- **Verification:** Add a real child-process regression that force-stops during
  verification, asserts the verifier and descendants are gone before STOPPED,
  and retains the existing bounded-timeout/descendant test.

## Gate 6 — Monitor projection and TUI contract

Reviewed the new `llm_tools/bridge_monitor.py`, its focused tests, and the TUI
projection adapter. The snapshot is now wired through the CLI and TUI adapters.
`python -m pytest tests/test_bridge_monitor.py tests/test_bridge_tui.py -q`
passed (21 tests), including the slow-snapshot keyboard responsiveness check.

### M1 — High: configured station settings can be presented as observed runtime state — CLOSED

- **Location:** `llm_tools/bridge_monitor.py` (`stations.observed`, lines
  188–204) and operation projection.
- **Initial evidence:** Runtime stores the scheduler's configured model pin in
  `operations.model`, not provider-reported model identity. The earlier monitor
  version presented it as `stations.observed.model` and called persisted
  operation state observed.
- **Fix:** The monitor exposes configured model separately and only reports an
  observed model when a verified `station-observation` event exists. It labels
  the database operation state separately and leaves provider liveness
  `unknown` without live evidence.
- **Verification:** `test_projection_uses_persisted_capacity_and_does_not_invent_opaque_values`
  checks the configured pin differs from unknown observation, then records an
  explicit verified observation.
- **Mode fix:** Snapshot and Systems view show `configured_mode` separately
  from `actual_mode`. The controller records a verified `controller-mode`
  event at startup, including the actual CLI override; absent that event, the
  actual mode remains `unknown`.
- **Verification:** The monitor test checks configured headless versus verified
  actual native mode and the unknown case. Actual provider model and liveness
  remain unknown unless separately observed.

### M2 — Medium: station and lifecycle projection do not match the TUI adapter — CLOSED

- **Location:** `llm_tools/bridge_monitor.py` (`stations`, `lifecycle`) and
  `llm_tools/bridge_tui.py` (`_bounded_snapshot`).
- **Initial evidence:** The first monitor version emitted a station mapping,
  while the adapter accepted only station lists; lifecycle was dropped.
- **Fix:** `project_snapshot()` now emits a bounded station list and explicit
  station context; `_bounded_snapshot()` preserves lifecycle and station
  context fields.
- **Verification:** `test_projection_uses_persisted_capacity_and_does_not_invent_opaque_values`
  passes the real monitor snapshot through `_bounded_snapshot()` and checks
  station rows and lifecycle data survive.

### M3 — Medium: authoritative latest facts disappear after event-history churn — CLOSED

- **Location:** `llm_tools/bridge_monitor.py` (`_latest_event`,
  `_latest_suspend_event`).
- **Initial evidence:** The first monitor version searched only the visible
  latest-100-event page for capacity, progress and controller facts.
- **Fix:** The visible event history remains bounded, and each authoritative
  event kind now uses a separate latest-row query. A >100-event churn test
  confirms selection, wait, task verification, controller and suspend facts
  remain available.
- **Index fix:** Schema v4 adds `(mission_id,kind,id)` and the latest-row
  queries explicitly use it. `_latest_suspend_event()` now performs four
  independent `ORDER BY id DESC LIMIT 1` lookups, one for each suspend kind,
  then chooses the greatest returned event ID. Each lookup uses the index
  without a temporary sort, so work stays bounded as event history grows.
- **Verification:** Event churn semantics pass in
  `test_latest_capacity_wait_and_progress_survive_more_than_one_hundred_events`.
  `test_latest_event_queries_use_mission_kind_id_index` checks each latest-kind
  query plan has no temporary B-tree. Independently reran the monitor and TUI
  suites: 21 passed.

### M4 — Medium: an older wait target overrides a newer selection — CLOSED

- **Location:** `llm_tools/bridge_monitor.py` (lines 156–164).
- **Initial evidence:** The earlier version inferred eligibility as the minimum
  future reset across all windows.
- **Fix:** It now uses persisted wait/decision targets rather than arbitrary
  window reset times. The churn test confirms a persisted wait target is used.
- **Fix:** The projection compares the event IDs and only uses a wait target
  when that wait is newer than the selection. A newer usable selection clears
  `next_eligible_epoch` and the old wait projection.
- **Verification:** `test_new_usable_selection_clears_older_wait_and_uses_readiness_target`
  covers stale wait followed by usable selection; the churn test checks the
  current wait target differs from individual window resets.

## Gate status

The final focused integration command
`python -m pytest tests/test_bridge_runtime.py tests/test_bridge_wait_interrupt.py
tests/test_bridge_recovery_tmux.py tests/test_bridge_force_stop.py
tests/test_bridge_native_e2e.py tests/test_bridge_dispatch_atomicity.py -q`
passed (35 tests). It covers normal O3-O8 lifecycle paths and exact native
resume across a controller restart; the injected tmux kill failure above is a
separate manual reproduction.

- Persistence gate: **CLOSED** — P1-P9, command atomicity, process identity,
  migrations and concurrent transactions passed the latest independent
  96-test persistence/lifecycle/CLI gate recorded below.
- Native terminal gate: **CLOSED** — T1-T6 passed at this checkpoint. CI
  configuration was reviewed locally; no CI run has been reviewed.
- Operator command dispatch: **CLOSED for O1, O2 and O5** — command effects are
  atomic, active task state survives exit, and cancellation reports deferred
  work with its operation still tracked.
- Ralph hook integration: **CLOSED for O3** — concurrent mission stop/task
  cancellation rejects launch without failing the mission; the task-cancel
  interleaving has manual evidence but no dedicated checked-in regression.
- Stop and wait lifecycle: **CLOSED for O4** — Bridge-aware Ralph and scheduler
  waits poll operator state; no-hook behavior remains unchanged.
- Controller recovery: **CLOSED for O8** — real tmux and injected teardown
  failure checks pass; failed cleanup preserves metadata and replacement
  readiness is not published.
- Graceful stop restoration: **CLOSED for O7** — verified terminal cleanup
  precedes STOPPED, including injected failure coverage.
- Verifier cancellation: **CLOSED for O10** — persisted verifier identity and
  owned descendant-tree teardown are exercised by force-stop/recovery tests.
- Native session identity safety: **CLOSED** — Codex is never auto-bound to a
  discovered session; Claude/Copilot IDs are Bridge-generated and matched to
  exact structured metadata before persistence. Actual provider restore remains
  **NOT VERIFIED**; current evidence is hermetic.
- Provider handoff, session reconstruction and final integrated gates have not
  yet been reviewed here.
- Monitor projection: **CLOSED for M1-M4** — configured and observed values are
  separated, the TUI adapter preserves the snapshot, and authoritative latest
  event projections use bounded indexed queries. No live model/liveness claim
  is made without a verified observation.
- No live provider capability is claimed by this review. Root's capability
  ledger remains authoritative and reports model execution as NOT VERIFIED.

## Independent full-spec audit, 2026-10-26

Reviewer: separate GPT-6.1 Sol context, High effort, as explicitly requested by
the user. Reviewed the directive, current handover, prior findings and current
production changes against `066af5c`, including uncommitted Bridge modules.
This section supersedes older blanket gate-closed claims where a finding below
reopens the gate. Production code is owned by the implementors.

### A1 — High: rejected mission cancellation commits partial effects — CLOSED

- **Location:** `bridge._apply_operator_command`, `_apply_operator_effect`, and
  `BridgeStore.set_task_status`.
- **Reproduction:** Create a running mission, commit a task exit with status 1,
  then cancel the mission. The command is REJECTED for `FAILED -> CANCELLED`,
  but the mission has already become CANCELLED. A later rejected transition can
  likewise preserve earlier effects in a multi-task cancellation.
- **Impact:** Command acknowledgement disagrees with authoritative state;
  rejected commands are not atomic, and failed work cannot be cancelled.
- **Required correction:** Permit explicit abandonment of failed tasks and
  roll back all command effects before committing a rejection acknowledgement.
- **Verification:** The first two tests in
  `tests/test_bridge_review_regressions.py` independently failed on the audited
  working tree. One uses actual failed-task state; one injects a transition
  rejection after the mission transition to enforce rollback generally.

### A2 — High: complete stop can leave owned descendants running — CLOSED

- **Location:** `bridge_runtime._terminate_owned_process_group`.
- **Reproduction:** An isolated process-group leader creates a child that
  ignores SIGTERM. The helper sends TERM and returns when the leader disappears;
  the child remains alive in the original owned process group.
- **Impact:** Force stop/recovery can report success while provider or verifier
  descendants continue changing the repository. Leader disappearance is not
  proof that managed work has stopped.
- **Required correction:** Verify termination of the established owned process
  group, with safe escalation for remaining members and PID/group-reuse checks.
- **Verification:**
  `test_owned_group_termination_kills_descendant_after_leader_exits` is an actual
  subprocess regression, independently RED. Its finally block cleans up only
  the process group created by that test.

### A3 — High: effective launch configuration is lost on restart — CLOSED

- **Location:** `bridge.start_mission`, `bridge_runtime.launch_controller`,
  `run_controller`, and `bridge_refresh.refresh_capacity_observation`.
- **Evidence:** `start` overrides for providers/routes/mode reach Ralph but do
  not update durable mission configuration. Refresh reads the original station
  rotation; a later `resume` restores it. An override from a native mission to
  headless can also conflict with the originally saved native flag.
- **Impact:** Capacity observation can describe a different rotation from the
  controller; shutdown/resume fails the configuration reconstruction contract.
- **Verification:**
  `test_controller_start_overrides_survive_complete_shutdown` independently
  failed after executing the real controller lifecycle with a bounded fake
  Ralph completion. No provider completion was invoked.

### A4 — High: core operator views do not expose their required information — OPEN

- **Locations:** `bridge_tui._bounded_snapshot`, `_ConsoleState.handle_key/rows`,
  `bridge_monitor.project_snapshot`.
- **Evidence:** Orders Enter dispatches an empty command, with no task details,
  dependencies, amendments or evidence. Ctrl-R opens an empty amendment file.
  Stations selection only stores a preference; the monitor hard-codes viewed
  station to `unknown` and renders configured rows without selected station
  history/restoration. Systems discards capacity decisions/windows during TUI
  projection, so actual quota/budget scopes never reach its renderer.
- **Impact:** The named seven views exist, but the required inspect/supervise
  workflow and provider-capacity presentation are incomplete (§§5–10,13).
- **Required correction:** Make task selection inspectable, present selected
  station/session history independently of execution, and preserve/render
  bounded capacity scopes. Verify via actual monitor-to-TUI projections and
  outer-PTY keyboard/mouse tests, not manually supplied already-correct rows.

### A5 — Medium: remaining traceability and monitoring gaps — OPEN

- No task-origin/proposal field, proposal acceptance workflow or corresponding
  test was found, despite §8.3 and the test matrix's assertion that proposals
  are not automatically scheduled.
- Stall detection covers committed nonzero exits only. No meaningful-change,
  repeated-identical-failure, reversal, elapsed-no-progress or provider-cycle
  detection is implemented beyond that failure count (§9.1).
- Headless `events --limit` and console `events` load all mission events before
  slicing/display; older logs cannot be paged through the TUI. The ordinary
  live snapshot itself correctly uses bounded indexed queries (§§13,18).
- Console `pause ID` and `resume ID` ignore the ID and affect the mission;
  task pause/resume are absent from the console dispatcher. `help` advertises
  `control`, which the dispatcher does not implement (§6).
- Existing progress and final-gate records remain dated/inconsistent with new
  modules and findings. Final CI/Python/coverage evidence must be recorded for
  one frozen production head, and every unverified live capability stays
  explicitly unverified.

Overall full-spec verdict at this audit checkpoint: **NOT MERGE READY**.
See `spec-audit.md` for the requirement and acceptance-scenario trace.

### A6 — High: existing attached scheduler loses terminal resize propagation — CLOSED

- **Location:** `scheduler.run_fresh_attached`.
- **Evidence:** The new unconditional `start_new_session=True` separates
  `script(1)` from the outer terminal's foreground process group. Resizing the
  real outer PTY from 80x24 to 120x40 and signalling its foreground scheduler
  group leaves the provider's child PTY at 80x24. Previously `subprocess.run`
  retained the shared group, so `script` received SIGWINCH and resized its PTY.
- **Impact:** Existing `llm-scheduler`/Ralph fresh attached behavior regresses;
  native terminal resize fidelity is a hard invariant, independently of Bridge.
- **Required correction:** Preserve ordinary attached job-control/resize
  behavior; isolate an owned process only where necessary and with explicit
  forwarding if required.
- **Verification:** Actual outer-PTY test
  `test_legacy_attached_scheduler_forwards_outer_terminal_resize` independently
  RED. No provider credentials or model invocation are involved.

### Reverification at `b11b719`, 2026-10-26

- `python -m pytest tests/test_bridge_review_regressions.py
  tests/test_bridge_force_stop.py tests/test_terminal_teardown_failure.py -q`
  passed all 14 cases before the additional A7 regression below.
- **A1 CLOSED:** failed-task cancellation succeeds; a rejected effect rolls back
  before its durable rejection acknowledgement. Both independent regressions
  pass after inspection of the exception/transaction boundary.
- **A6 CLOSED:** ordinary attached execution retains its foreground group;
  the actual outer-PTY resize regression passes.
- **A2 CLOSED for same-process-group descendants:** the group-membership scan
  now waits for descendants and escalates after leader exit; actual subprocess
  regression passes. A7 below covers the distinct independent-session case.
- **A3 PARTIAL:** provider/mode overrides are persisted and the basic shutdown
  regression passes. Prior configuration must still be retained for recovery:
  the current controller overwrites `headless` before deciding whether to stop
  an old tmux station. Explicit mode switches also need unambiguous semantics.
  These concerns were sent to the root before final closure.

### A7 — High: independent-session owned tools survive complete stop — CLOSED

- **Location:** `bridge_runtime._terminate_owned_process_group` and process
  ownership metadata/recovery.
- **Runtime observation:** The real Codex 0.164.0 daemonless native probe
  created MCP tool subprocesses in separate process groups. This is normal
  provider behavior, not a hypothetical detached unrelated session.
- **Independent reproduction:** A provider fixture starts a TERM-ignoring
  child using `start_new_session=True`. Complete-stop helper returns after
  killing the original group while the owned child remains alive.
- **Impact:** Managed tool processes may continue running after successful
  force-stop/recovery. Group membership alone is insufficient for the complete
  managed-process shutdown contract.
- **Required correction:** Track and terminate verified owned descendants,
  including independent sessions, without signalling unrelated or reused PIDs.
  Preserve sufficient identity before losing parent ancestry during shutdown.
- **Verification:** New actual-subprocess regression
  `test_complete_stop_terminates_owned_descendant_in_a_new_session` independently
  RED. Its cleanup validates the exact child start token and signals only
  fixture-created groups. Root and main implementor were notified.

### Owned Codex native identity observation (no transcript capture)

- Root's native probe supplied its exact root PID 125853/start tick 40682190,
  launched with `--no-daemon` in `/tmp/lb-native-owned/work`.
- The reviewer checked that root identity before and after the read-only probe.
  Its direct native Codex child PID 125873/start tick 40682195 held writable
  rollout descriptor 61. Only the first structured `session_meta` record was
  read; no messages, credentials or transcript contents were logged.
- The metadata identified UUID `01a17795-0e6a-7a00-9080-e5db0c68a535`, matching
  CWD, source `cli`, originator `codex-tui`. Exactly one owned writable rollout
  identity was found. This establishes process-bound provenance and avoids a
  global recency/CWD lookup. Implementation must retain ambiguity/missing-data
  fallback and recheck process start tokens.
- Installed protocol schemas were generated without model calls. The official
  [App Server documentation](https://learn.chatgpt.com/docs/app-server) also
  provides `thread/start` returning an exact ID; it is a possible alternative,
  but this review did not execute a precreate/resume protocol experiment.
- Root reported `NATIVE_BRIDGE_OK` from the live reply. That reply and native
  resume/mouse evidence belong to the root's live-probe ledger; the reviewer
  independently establishes only the owned-FD identity facts above.

### Changed-surface re-review after `b11b719`, 2026-10-26

This review covers the subsequent working-tree fixes. It is not final approval
of an unfrozen release head.

- **Independently passed:** 82 tests in 16.72 seconds across reviewer
  regressions, typed native sessions, real-controller fake native restart,
  runtime, embedded Usage/cache/rendering, TUI and attached outer PTY suites.
  Command: `python -m pytest tests/test_bridge_review_regressions.py
  tests/test_bridge_sessions.py tests/test_bridge_native_e2e.py
  tests/test_bridge_runtime.py tests/test_bridge_usage.py
  tests/test_usage_embedded.py tests/test_bridge_tui.py
  tests/test_terminal_attached.py -q -o addopts=''`.
- **Independently passed:** a further 96 tests in 12.35 seconds using
  `python -m pytest tests/test_bridge_store.py tests/test_bridge_concurrency.py
  tests/test_bridge_dispatch_atomicity.py tests/test_bridge_force_stop.py
  tests/test_bridge_recovery_tmux.py tests/test_bridge_verifier.py
  tests/test_terminal_teardown_failure.py tests/test_bridge_cli.py
  tests/test_bridge_cli_acceptance.py tests/test_bridge_steering.py
  tests/test_bridge_store_boundaries.py -q -o addopts=''`.
- **A3 CLOSED:** the controller now retains prior station mode for recovery,
  cleans up before persisting a new effective mode, and makes explicit native
  versus headless overrides mutually exclusive. Both basic persistence and
  prior-mode teardown-failure regressions pass.
- **A7 CLOSED:** a bounded process-tree snapshot records descendant start
  tokens before signalling; separate-session groups and surviving descendants
  are terminated with identity checks. The actual separate-session fixture
  passes, alongside the prior leader-exit fixture.
- **O7/O8/O10 CLOSED:** stop/recovery preserve terminal metadata on failed
  teardown and do not publish STOPPED/replacement readiness first; verifiers
  have persisted process identity and owned tree cleanup. Runtime, force-stop
  and teardown fault cases have independently passed.
- **A4 PARTIAL:** task detail, amendment preload, preserved capacity scopes and
  scope forecasts now exist and their targeted tests pass. Selected-station
  projection and detail scrolling remain assigned follow-up work; this High
  finding remains open until those actual workflows are verified.
- **A5 PARTIAL:** explicit proposal provenance/acceptance now exists, operator
  commands share public parser/handlers, and events use bounded SQL paging.
  Root owns remaining monitor/stall/history evidence and final ledger refresh.
- The reviewer inspected the stronger root-owned full native mission probe
  and `native-mission-results.json`. It checks a real native reply, complete
  controller shutdown, the same owned UUID with RESTORED_NATIVE after a new
  controller, and a new reply recalling prior context. Its response oracle
  distinguishes the first and resumed marker and rejects substring matches.
  Codex native reply/resume are now supported by live evidence; provider mouse
  remains explicitly NOT VERIFIED. No paid calls were initiated by this reviewer.

### A8 — Medium: accepted follow-up can be stranded in a terminal mission — CLOSED

- The independent temporary-store reproduction accepted a pending proposal
  after mission completion, returning APPLIED with an ACCEPTED/QUEUED task in a
  COMPLETED mission that cannot resume. Ordinary task additions had the same
  issue.
- `add_task`, `propose_task` and proposal acceptance now reject COMPLETED and
  CANCELLED missions before mutation. Two independent parameterized review
  regressions pass; failed acceptance preserves PENDING state.

### A9 — Medium: public proposal options are rewritten as compact syntax — CLOSED

- The reviewer independently reproduced `proposal accept --task ID` failing
  inside the REPL while being valid standalone grammar. The positional alias
  rewrite treated `--task` itself as the task ID.
- The rewrite now applies only to a positional third token. Independent
  acceptance/rejection tests using public option syntax both pass, including a
  quoted rejection reason. The original standalone parser still validates it.

### A10 — Medium: embedded Usage can hide a configuration exit — CLOSED

- `EmbeddedUsage._refresh` catches Exception while the existing configuration
  loader deliberately raises SystemExit for invalid configuration. The worker
  then exits through `finally` with no recorded error; an empty snapshot can
  incorrectly report ready.
- Root owns the boundary fix and regression: report unavailable/configuration
  error without blocking input or printing provider/config payloads. The
  already-tested ordinary Exception path retains an explicitly stale result.

### A11 — Medium: repository activity fingerprint ignores executable changes — CLOSED

- **Location:** `bridge_progress.repository_checkpoint`.
- The new bounded fingerprint hashes names and content but omits executable
  mode. Independently changing an existing script from 0644 to 0755 yields the
  same nonempty fingerprint, so meaningful executable changes can contribute
  to a false repeated-unchanged warning.
- Include the relevant file mode in activity evidence and race validation.
  `test_repository_progress_observes_executable_mode_changes` independently
  fails using a real disposable Git repository; no provider calls are involved.

### A12 — Medium: unaccepted proposals prevent normal mission completion — CLOSED

- **Location:** `BridgeMissionHooks.before_iteration` completion count.
- `ready_tasks`, monitor counts, verification criteria and the mission's legal
  COMPLETED transition exclude unaccepted proposals. The controller's final
  task count still includes them, so all accepted work can be verified while
  the controller waits forever on a proposal that must not be scheduled.
- Apply the same accepted/operator work predicate to the controller's
  completion decision. Pending proposals remain inspectable without becoming
  required work.
- `test_unaccepted_proposal_does_not_prevent_verified_mission_completion`
  independently fails against actual store/hook state; its wait guard prevents
  a hanging test and does not replace the scheduling/count logic.

### A13 — High: unchanged editor roundtrip silently truncates task instructions — OPEN

- **Location:** `bridge_tui._run` Ctrl-R path, `_bounded_task_value` and
  `_edit_task_prompt`.
- Ctrl-R preloads the display projection's prompt, which is truncated to
  16,000 characters and has tabs/carriage returns replaced. Saving without
  changing anything submits that lossy projection as the next durable prompt.
- **Independent reproduction:** A real curses control process, real SQLite
  mission and shared CLI dispatcher use `/bin/true` as a no-op editor. A prompt
  over 20,000 characters loses its tail, tab and CRLF in the persisted amendment.
  `tests/test_bridge_long_prompt_editor.py` is independently RED in 0.39s.
- **Impact:** Later execution silently receives incomplete or modified user
  instructions even though the operator made no content changes.
- Fetch full verbatim task text on edit through an appropriate task-read
  callback/public CLI, preserving newlines when writing the private editor
  file. Keep display projections bounded and sanitized for rendering only.
  The terminal implementor has the regression and owns remediation.

### Activity/monitor/Usage follow-up verification

Independent command `python -m pytest tests/test_bridge_progress.py
tests/test_bridge_review_regressions.py tests/test_bridge_monitor.py
tests/test_bridge_refresh.py tests/test_bridge_usage.py tests/test_usage_embedded.py
-q -o addopts=''` passed **46 tests in 2.36s**.

- **A10 CLOSED:** embedded refresh catches the configuration loader's
  SystemExit at its background boundary and records unavailable state.
- **A11 CLOSED:** executable permission bits are now part of the repository
  fingerprint; the independent chmod regression passes.
- **A12 CLOSED:** controller completion counts accepted/operator work only;
  the independent pending-proposal hook regression passes.
- The inspected convergence module bounds retained evidence to 32 observations,
  returns unknown for incomplete scans, and distinguishes unchanged boundaries,
  identical failures, provider cycling and repeated reversals. A single long
  operation is not called stalled. Repository evidence remains separate from
  task completion evidence.
- Actual capacity refresh now records observations through the shared usage
  sample helper before estimating. The non-mock test creates two real history
  samples and obtains the expected estimate without opening Usage or relying on
  an optional service. Model-specific and delegated pool labels are preserved.
- Selected-station preference, historical operation metadata, elapsed time,
  iteration and progress age now reach the real monitor snapshot. Final TUI
  behavior and A13 editor preservation remain under terminal review.
