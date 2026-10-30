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

### A4 — High: core operator views do not expose their required information — CLOSED

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

### A5 — Medium: remaining traceability and monitoring gaps — CLOSED

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

### A13 — High: unchanged editor roundtrip silently truncates task instructions — CLOSED

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

### Shared-handler/editor/log follow-up at `37cbc2b` plus working tree

- **A13 CLOSED:** the editor now obtains full task text through the shared
  public task-show path, validates its identity/type, and writes with
  `newline=''`. The independent actual-curses 20k/tab/CRLF no-op editor
  regression passes. Display projection remains bounded and sanitized.
- A focused editor/CLI/refresh/monitor/TUI/outer-PTY run passed **88 tests**;
  its only failure was the root's already-known explicit-mission grammar
  interpretation test, which is being replaced to match the user's complete
  CLI-parity requirement. Both explicit mission spellings should select the
  requested mission; omitted mission defaults to the active one.
- Shared `_task_result` is used by standalone and REPL task paths. This removes
  interpreter startup from common interactive task operations while preserving
  the same parser, validation, durable transactions and rejection results.
- New coverage tests were inspected for behavioral assertions. They cover
  rejection without partial effects, immutable process identity, concurrent
  command prevention, stale-snapshot retention, editor cleanup, real private
  socket ownership and bounded projection. These supplement rather than
  replace actual subprocess/PTY/live evidence.

### A14 — Medium: older log navigation can move forward during live output — CLOSED

- **Location:** `bridge_monitor.project_snapshot` event OFFSET and
  `bridge_tui._ConsoleState` log paging.
- OFFSET is relative to the newest event on each refresh. After viewing IDs
  52–71, 25 new events followed by PgUp returns IDs 57–76: the purported older
  page is newer than the page the operator was reading. Older views also drift
  as live events arrive.
- Anchor historical browsing to the visible event-ID boundary/cursor; return
  to the live head deliberately when paging back to newest. Keep latest
  capacity/progress projections independent of the historical event page.
- `test_log_older_page_stays_older_when_live_events_arrive` independently fails
  through the real console state, production snapshot callbacks and SQLite.
  The test asserts user-visible chronological navigation rather than a
  particular cursor representation.
- **Independent closure, 2026-10-27:** Historical paging now freezes the visible
  maximum event ID and applies offsets within that fixed boundary; paging back
  to offset zero explicitly resumes the live head. The original real
  console/SQLite append regression passes. Latest capacity/progress queries
  remain independent of the paged event query.

### A15 — High: in-process task stdin reads capture the control terminal — CLOSED

- **Location:** `dispatch_console_command` in-process `_task_result` branch and
  `read_verbatim('-')`.
- The standalone CLI intentionally reads stdin for `--file -`. In the REPL,
  the new in-process task handler now reads the same raw terminal as curses.
  The worker waits for EOF while stealing input; command-pending state then
  prevents normal Ctrl-D exit. Previously the CLI child had stdin DEVNULL.
- **Independent actual-PTY reproduction:** Start control, type
  `task add --file -`, then repeated Ctrl-D. The control process remains alive
  and stuck. `test_real_curses_stdin_file_request_keeps_control_input_available`
  is independently RED in 2.27s; its finally block terminates only its own
  disposable control process. No provider is started.
- Route interactive stdin entry through a deliberate multiline/editor input
  flow, or reject it clearly before any read and direct the operator to the
  equivalent supported input flow. Never let a background handler read the
  active curses stdin. Preserve standalone CLI stdin behavior.
- **Independent closure, 2026-10-27:** The shared public parser now resolves the
  command first, and the REPL rejects `prompt_file == '-'` before any handler
  can read stdin, with a Ctrl-E multiline input hint. The actual curses PTY
  regression now exits normally using Ctrl-D. Standalone CLI input handling is
  unchanged; no string-only option guards remain.

### A16 — High: ordinary stop does not stop a completed mission's session — CLOSED

- **Location:** `_apply_operator_effect` stop handling and completed-controller
  terminal lifecycle.
- Completion correctly ends the controller while leaving its private terminal
  available for inspection. A later ordinary stop only changes RUNNING/PAUSED
  missions; for COMPLETED it returns success without terminating the retained
  tmux server, Bridge UI or autonomous log-tail process. With no controller
  remaining, nothing consumes that stop request.
- **Independent real-controller reproduction:** `/bin/true` provider fixture,
  local verifier, private tmux, mission COMPLETED and ownership lock released.
  Ordinary `stop` returns 0 while the session and UI remain alive. The new
  `tests/test_bridge_completed_stop.py` independently fails in 0.63s; cleanup
  uses only that disposable mission's forced stop and private backend.
- Stop must reconcile session resources independently of mission completion
  state/controller presence. Preserve verified mission results while closing
  the owned session, rather than requiring an undocumented force-stop step.
  Also avoid acknowledging a no-op stop for other inactive-owner terminal
  states. Do not infer absence of owned processes merely from mission status.
- **Independent closure, 2026-10-27:** Ordinary stop now acquires the mission
  ownership lock, rechecks terminal status, and stops the exact owned station
  while retaining the terminal mission result. The original actual-controller,
  private-tmux completion/stop regression passes. An active owner prevents this
  separate cleanup path from racing controller resource management.

### Original operator/monitor gap closure

The original A4/A5 implementation gaps are now closed: task detail and separate
detail scrolling, verbatim amendment loading, selected versus executing station
projection and history, native restoration metadata, shared public commands,
proposal provenance, scoped capacity forecasts and the full embedded Usage
dashboard are implemented with inspected targeted tests. Indexed bounded event
and convergence queries, elapsed/iteration/progress-age fields and actual
sample-history warming address the earlier monitoring gaps. The two subsequent
issues are tracked separately as A14 (stable historical log navigation) and A15
(terminal-safe interactive stdin). Final ledger/documentation/coverage/CI
evidence is still required on one frozen release head.

### A17 — High: Claude native transcript format does not match its confirmation parser — CLOSED

- **Location:** `bridge_sessions.provider_session_metadata` and
  `_session_fields('claude', ...)`.
- The adapter reads Claude's native `projects/<encoded-cwd>/<planned-uuid>.jsonl`
  but only recognizes the `system.init` record emitted by the separate
  stream-JSON output protocol. A bounded, read-only inspection of metadata keys
  in three installed historical native transcript files found top-level
  `sessionId` and `cwd` on `user`/`assistant` records. No message content,
  credential values or session identities were printed or bound to Bridge.
  The existing parser rejects these records, so the fake-native initialization
  tests can pass while real Claude confirmation remains unavailable.
- Project-directory encoding is also different: among eight bounded historical
  files, slash-only encoding matched five directory names while replacing each
  non-ASCII-alphanumeric character with `-` matched all eight. The fixture uses
  an underscore/dot CWD and this observed encoding, rather than perpetuating
  the fake adapter's slash-only path assumption. Only aggregate match counts
  were printed; no historical paths or session values were retained.
- `tests/test_bridge_native_metadata_review.py` supplies an anonymized native
  transcript-shaped fixture at the exact ID-addressed path. Its positive
  confirmation assertion independently fails (one failure in 0.08s). The same
  test requires rejection of mismatched session ID and working directory.
- Support the observed transcript metadata only with exact planned UUID,
  matching CWD and provider-file provenance. Retain rejection of generic text,
  unrelated sessions and filename-only identity inference. The narrower
  stream-init parser may remain separately valid for stream output. No paid
  Claude completion is authorized or needed to verify the metadata adapter;
  live native Claude reply/resume must remain explicitly unverified.
- Copilot's three bounded historical files expose `session.start`,
  `data.sessionId` and `data.context`, consistent with the current adapter.
- **Independent closure, 2026-10-27:** Native Claude user/assistant metadata is
  now accepted with source `claude:transcript`, exact planned ID and matching
  canonical CWD, using the observed nonalphanumeric project encoding. Legacy
  stream-init identity remains valid independently. Sidechain and malformed
  type records are rejected without exceptions. The original anonymized
  metadata regression and updated controller/restart fake-native fixtures pass.
  This verifies the adapter, not live paid Claude reply/resume.

### A18 — Medium: plain start discards a mission's stored execution mode — CLOSED

- **Location:** `bridge._build_parser` start mode defaults and `main` start
  argument resolution.
- `mission create --native` and `mission create --headless` persist their
  execution choices, but later `start --mission ID` always sees both argparse
  `store_true` fields as `False`. The `getattr(..., config.get(...))` fallback
  never runs because those fields exist. Plain start consequently launches
  autonomous tmux mode and overwrites either stored choice. This also affects
  the shared REPL start command.
- `tests/test_bridge_start_mode_review.py` independently fails both persisted
  mode cases (two failures in 0.14s). It uses the real durable store and public
  parser/handler, replacing only the final launch boundary to avoid provider
  calls. Existing lifecycle tests covered explicit `--headless`, so they did
  not exercise omission.
- Distinguish omission from an explicit mode override. With no mode argument,
  inherit the stored pair; an explicit `--native` or `--headless` selects that
  mode exclusively. Retain rejection of explicitly conflicting flags.
- **Independent closure, 2026-10-27:** Public start flags now preserve omission
  as `None`, and the launch controller resolves both stored fields together.
  Explicit native/headless selection clears the opposite stored mode; both
  explicit flags remain invalid. Updated tests inspect final controller argv
  across omitted and explicit selections, rather than requiring resolution at
  a particular helper boundary. Independent start/CLI/runtime gate: **158 tests
  passed in 4.22s**.

### A19 — Medium: same-second operations can report an older execution as latest — CLOSED

- **Location:** `bridge_monitor.project_snapshot`, bounded operations query.
- Operation timestamps have second precision and their primary keys are
  random UUIDs. Sorting by `created_at DESC,id DESC` therefore orders rapid
  attempts by random ID rather than their insertion/launch sequence. After
  the second operation exits, the monitor can report iteration 1, the older
  station/model and an older restoration state. The active-operation filter
  hides this in tests that leave the second operation RUNNING.
- `tests/test_bridge_monitor_chronology_review.py` independently fails two
  same-second cases (different stations and repeated station), using the real
  store and monitor with deterministically reverse-lexicographic UUIDs. The
  first failure reports iteration 1 instead of 2; additional assertions cover
  last-launched station, newest configured model, history order and restoration
  state. Result: two failures in 0.11s.
- Use a real insertion sequence for operation chronology. This SQLite table
  has a TEXT primary key and retains rowid, so a bounded descending rowid query
  can supply that sequence without guessing chronology from UUID text.
- **Independent closure, 2026-10-27:** The query now uses descending rowid and
  a mission-prefix history index to retain bounded lookup. Both original
  same-second fixtures pass, including latest station/model/restoration state.
  Independent chronology/monitor/store/start/owned-pane gate: **69 tests passed
  in 2.29s**. Root is separately checking FIFO command and handoff histories for
  the same class of timestamp/UUID tie before final freeze.

### A20 — Medium: owned-pane timeout repair changes explicit legacy bounds — CLOSED

- **Location:** `scheduler.run_tmux`, timeout sentinel and wait condition.
- Root identified the pre-existing implicit one-hour timeout on owned native
  provider panes. Removing that implicit bound is appropriate because Bridge
  owns their complete lifecycle. The initial repair uses numeric zero as the
  unbounded sentinel and treats all `timeout <= 0` as unbounded, which also
  changes explicit legacy `LLM_SCHEDULER_TMUX_TIMEOUT=0` or `-1` from an
  immediate timeout to an indefinite wait.
- `tests/test_scheduler_timeout_review.py` independently fails both explicit
  legacy values (two failures in 0.11s). A provider fixture completes after one
  poll so even the erroneous unbounded wait stays finite. The assertion
  requires the original timeout result 124 before entering a polling wait.
- Use a distinct absent/unbounded sentinel for the owned-pane default, retaining
  the numeric comparison for explicitly configured values and the existing
  legacy default. Existing positive override and long owned-pane tests should
  continue passing.
- **Independent closure, 2026-10-27:** The absent owned-pane timeout is now
  represented by `None`; all explicit integers retain the original numeric
  comparison. The original zero/negative regressions, positive owned timeout,
  beyond-one-hour default owned wait and actual owned-pane tests pass:
  **12 tests in 1.73s**. Only Bridge's implicit native bound is removed; the
  legacy default and explicit bounds retain their prior behavior.

### Independent delta verification, 2026-10-27

`python -m pytest tests/test_bridge_review_regressions.py tests/test_bridge_long_prompt_editor.py tests/test_bridge_completed_stop.py tests/test_bridge_cli.py tests/test_usage_embedded.py -q -o addopts=''`
passed **50 tests in 6.24s**. This covers the original A14/A15/A16 reproductions,
the full verbatim editor, explicit mission argument forms, shared task handler
and the embedded dashboard's disabled ephemeral service launch. The new A17
regression remains separately red pending the provider metadata fix. Interactive
attach is still under implementation and is not approved by this checkpoint.

An additional independent run of monitor/refresh/Usage/progress/native-input
oracle tests passed **42 tests in 1.00s**:
`python -m pytest tests/test_bridge_monitor.py tests/test_bridge_refresh.py tests/test_bridge_usage.py tests/test_usage_embedded.py tests/test_bridge_progress.py tests/test_bridge_native_input_probe.py -q -o addopts=''`.
The new wheel oracle checks movement of a known smoke marker and tmux pane mode
zero before and after the event. Checked-in evidence honestly distinguishes
Bridge wheel movement from unverified direct wheel/click behavior; it does not
claim that a screen redraw alone proves provider mouse support.

### Final integrated delta review, 2026-10-27

The native metadata and completed interactive terminal changes were inspected.
Terminal transitions are typed worker results, executed after curses restores
the terminal. Embedded transitions verify the source socket and Bridge pane;
cross-station attachment detaches only a selected writable client, preserves
both servers and returns that client to the source when the destination
detaches. Read-only/control clients are excluded, multiple writers require an
explicit listed client, and standalone detach uses an exact client selector.
Prefix/status focus explicitly removes zoom before selecting the target pane.

The independent integrated command passed **85 tests in 19.91s**:
`python -m pytest tests/test_bridge_native_metadata_review.py tests/test_bridge_session_boundaries.py tests/test_bridge_sessions.py tests/test_bridge_native_e2e.py tests/test_terminal_interactive_attach.py tests/test_terminal_attached.py tests/test_bridge_cli.py tests/test_bridge_long_prompt_editor.py tests/test_bridge_review_regressions.py tests/test_bridge_completed_stop.py -q -o addopts=''`.
This includes actual outer-PTY cross-station/return and selected-client detach,
curses restoration, exact owned native IDs, malformed metadata, real completed
controller stop and original input/prompt regressions. The newer A18 test is
separately red and is not represented by that passing result.

Added coverage tests were inspected for rejection/state/process assertions;
they do not replace actual PTY and controller evidence. Remaining release gates
are A18 resolution, final whole-suite/global85/Bridge90 results on the final
head, honest documentation and the root-owned bounded live probe. No paid model
call or production change was made by the reviewer.


### Final independent integration review of `41cff55`, 2026-10-27

Sol 6.1 High independently reread the original directive and accepted additions,
inspected the current chronology, shared REPL/response overlay, process/session
restoration and live evidence oracles. No production or test file was edited
during the root-owned frozen coverage runs; no paid model call was made by
this reviewer. Original directive `docs/features/llm-bridge/prompt.md` is preserved.

The independent focused command was:

```text
python -m pytest tests/test_bridge_chronology_contracts.py tests/test_bridge_monitor_chronology_review.py tests/test_bridge_session_boundaries.py tests/test_bridge_native_metadata_review.py tests/test_bridge_sessions.py tests/test_bridge_native_e2e.py tests/test_bridge_start_mode_review.py tests/test_bridge_cli.py tests/test_bridge_tui_projection_contracts.py tests/test_bridge_tui_error_contracts.py tests/test_bridge_live_probe_contracts.py tests/test_bridge_native_input_probe.py tests/test_terminal_interactive_attach.py tests/test_terminal_attached.py tests/test_bridge_completed_stop.py -q -o addopts='' --basetemp=/tmp/llm-tools-final-review-41cff55
```

Result: **122 passed in 16.22s**. This confirms targeted chronology, shared
commands and bounded scrollable responses, effective mode restoration, owned
metadata checks, real attached/cross-station terminal transport and completed
controller cleanup. It does not establish paid provider compatibility or replace
the final global85/Bridge90 and Python 3.11/3.15 gates.

### A21 — High: owned Codex discovery parses multiple JSONL records as one object — CLOSED

- **Location:** `bridge_sessions.discover_owned_codex_session`, bounded
  `os.pread` and `json.loads` at the first-record read.
- The function reads up to 65,537 bytes from an owned live transcript descriptor
  and parses the complete prefix as one JSON object. A normal JSONL transcript
  with a valid `session_meta` first record and any second record is rejected as
  extra JSON data. A later large record also makes the total prefix exceed the
  length guard despite a short valid first record. Session discovery therefore
  depends on observing the tiny interval before Codex appends another record.
- An independent disposable actual child held a writable FD below its temporary
  `CODEX_HOME/sessions`, with validated PID/start token and matching CWD. A
  132-byte valid header alone returned an identity; appending a single event
  produced a 191-byte file and returned none; appending a 70k later event also
  returned none. No historical user transcript was inspected or attributed.
- Existing owned-FD regression fixtures write only one record, so their passing
  results mask the native JSONL shape. Earlier isolated live successes cannot
  close this deterministic failure; the latest root-owned native retake also
  remains unverified. This blocks exact-owned Codex restoration acceptance.
- Bound and parse the first newline-delimited record independently of the
  complete transcript length, preserving inode/FD/process/CWD provenance and
  rejection of genuinely oversized/malformed headers. Add actual-owned-FD
  regressions for two records and a transcript larger than 64KiB, then rerun
  the bounded live reply/full-stop/exact-ID-resume probe.

### A22 — Medium: live mission probe admits task two before its intended full-stop boundary — CLOSED

- **Location:** `scripts/bridge_native_mission_smoke.py`, second task creation
  before `exit_provider(first)` and subsequent stop request.
- The second task is ready while task one is active. As soon as task one exits
  cleanly to VERIFYING, the production `before_iteration` hook can immediately
  select task two, before the probe's polling client observes VERIFYING and
  sends stop. This can launch the recall prompt on the first controller, spoil
  the expected two-operation proof or exceed the intended two-prompt launch
  sequence. A matching first reply is also followed by one immediate config
  read rather than a bounded wait for persisted owned identity.
- A disposable real store/hook reproduction confirms that after task one
  enters VERIFYING, the already-added second task is immediately eligible and
  selected while the mission remains RUNNING. This is legal production behavior
  and a missing synchronization boundary in the probe.
- Pause or stop scheduling before exiting the first operation, prove controller
  release and full stop, and add the recall task only after that boundary. Wait
  boundedly for persisted owned identity after the first response. Preserve
  strict exact-response, same-ID, new-controller and cleanup oracles; never
  turn absent responses into startup/geometry passes. Root owns this repair.


**A21/A22 independent closure, 2026-10-27:** The owned-FD reader now partitions
the bounded prefix at the first newline before length checking and JSON parsing.
Four new actual writable-FD tests cover later second/large records and reject
oversized/malformed headers. Exact PID/start-token, FD/inode, unique identity and
CWD checks remain. The probe boundedly waits for persisted identity, pauses
scheduling before exit, stops and proves controller-lock release, then adds the
recall task. The independent session/metadata/native-E2E/oracle command passed
**49 tests in 15.68s**:

```text
python -m pytest tests/test_bridge_sessions.py tests/test_bridge_session_boundaries.py tests/test_bridge_native_metadata_review.py tests/test_bridge_native_e2e.py tests/test_bridge_live_probe_contracts.py -q -o addopts='' --basetemp=/tmp/lb-final-review-native-fixed
```

The reviewer inspected root-owned `/tmp/bridge-resume-native-fixed.json`, produced
by the repaired opt-in live two-prompt probe with Codex 0.164.0: initial reply
PASS, exact same owned UUID/new-controller/context-recall PASS, first controller
released, cleanup PASS. The strict response/identity/operation-count oracles
remain intact; provider mouse is still NOT VERIFIED. The reviewer made no paid
call and did not infer a universal provider capability from this result.

### A23 — Medium: attached terminal acceptance has a reproducible intermittent focus failure — CLOSED

- **Location:** `tests/test_terminal_attached.py`, real outer-PTY prefix/status
  focus sequence; final CI terminal gate.
- Root's PR Python3.15 run failed after one status-left click while zoomed; the
  push job passed. Its dead-server test also sampled the external kill-server
  shutdown before death was observable. One successful job does not resolve
  either timing-sensitive result.
- The reviewer repeated the unchanged attached-client and dead-server tests
  with distinct basetemps. Runs 1–6 passed both; run 7 failed earlier at
  `prefix_key('P')` line220: provider pane `%0` expected, active pane `%1`, zoom
  false. The fake provider/controls byte logs are retained under
  `/tmp/lb-final-review-race-7/test_attached_client_input_pas0/`. Teardown passed
  all seven iterations. This establishes instability broader than a specific
  status-click assertion, without yet attributing its root cause to production.
- Investigate actual input/event ordering and terminal readiness; retain a
  single successful deliberate prefix/click as the acceptance contract. Do not
  retry focus actions until they happen to pass. The external-death fixture may
  boundedly wait for its stated dead-server precondition, preserving refusal to
  discard ownership metadata while state is UNKNOWN. The terminal implementor
  owns remediation and the reviewer will rerun the corrected test repeatedly.


The independent broader integration command passed **80 tests in 46.82s**:

```text
python -m pytest tests/test_bridge_tui.py tests/test_bridge_long_prompt_editor.py tests/test_bridge_steering.py tests/test_bridge_dispatch_atomicity.py tests/test_bridge_concurrency.py tests/test_bridge_progress.py tests/test_bridge_monitor.py tests/test_bridge_refresh.py tests/test_bridge_usage.py tests/test_bridge_stress.py tests/test_bridge_long_run.py -q -o addopts='' --basetemp=/tmp/lb-final-review-final-integration
```

This covers response scrolling, durable task edits, concurrent commands,
convergence, asynchronous capacity/Usage, large queues and simulated iteration
history. The following independent adversarial response case was not present in
that passing suite.

### A24 — Medium: failed task inspection is mistaken for a rejected command response — CLOSED

- **Location:** `bridge_tui._command_feedback`, generic status-based envelope
  detection.
- Any dictionary with `status` APPLIED/REJECTED/FAILED is summarized as a durable
  command acknowledgment, without checking that the object is a command
  envelope. `task show` returns a task object directly. A task in the legitimate
  FAILED state is therefore reduced to `Rejected: failed`, discarding its ID,
  prompt, evidence and failure context. The standalone CLI displays those
  fields, so this also breaks the accepted REPL-response parity requirement.
- Independent reproduction: pass a JSON task object with `id:7`, `status:FAILED`,
  title and prompt into `_command_feedback('task show --task 7', ...)`; current
  output is `Rejected: failed` instead of the inspectable task record.
- Require actual command-envelope structure before acknowledgment formatting.
  Retain FAILED task objects as bounded multiline inspection results, and add
  a regression alongside the existing envelope summaries and response overlay
  checks. No production/test change was made by the reviewer.


**A24 independent closure and REPL option parity, 2026-10-27:** Command feedback
now requires a `result` member before treating status as a command envelope.
A FAILED task remains a multiline task record; failed command envelopes retain
their rejection reason. Explicit `usage`/`control` mission options now use the
public parser and a typed control-view action. Same-mission actions choose and
expand the local view; other-mission actions open after curses has restored the
terminal. Unknown options are rejected and public help remains available.
The source boundary and new task/mission-option/transition regressions were
independently inspected.

```text
python -m pytest tests/test_bridge_cli.py tests/test_bridge_tui.py tests/test_bridge_tui_projection_contracts.py tests/test_bridge_tui_error_contracts.py tests/test_bridge_live_probe_contracts.py tests/test_bridge_native_input_probe.py -q -o addopts='' --basetemp=/tmp/lb-final-review-response-fixed
```

Result: **89 passed in 2.89s**. `git diff --check` was clean at that checkpoint.

**Native input comparison correction:** The earlier direct/Bridge wheel
comparison used unequal provider heights, so history could fit in one viewport
and scroll in the other. The probe now measures Bridge's provider dimensions
and uses the same dimensions for the direct single-pane tmux comparison, runs
sequentially to avoid two writers to one session, and recognizes the actual
strict wheel oracle's PASS result. The reviewer inspected
`final-native-input.json`: Codex0.164.0, both provider viewports120x32, both known
marker rows move `(3,6,14)` to `(5,8,16)`, pane mode zero before/after, local model
picker input PASS, cleanup PASS. This supports observed native wheel parity
within these tested tmux paths. Provider-click selection still has no visible
effect in either path and remains NOT VERIFIED; Claude2.1.310 local help only
is verified, with paid completion/resume and mouse unverified. These limits
replace the older unequal-geometry DIFFERENT wheel result.


**A23 independent closure, 2026-10-27:** Status targets now activate on mouse
release, keeping the completed click from changing focus again after an unzoom
operation. Native viewport down/up/wheel bindings remain intact. The real outer
PTY test waits for stable focus after one action, then proves routing with
unique bytes at the selected child. Its former scan over right-status columns
is replaced with one measured click. Stable-wait timeout always fails unless
the required interval was observed. Exact custom release/unzoom/target bindings
are asserted independently of tmux defaults. The external kill-server fixture
boundedly waits for observable death, retaining UNKNOWN refusal in production.
The event-order diagnosis is supported by source inspection and corrected
repeat outcomes; no universal timing guarantee is inferred from the test run.

The independent frozen terminal command passed **48 tests in 9.60s**:

```text
python -m pytest tests/test_terminal_backend.py tests/test_terminal_attached.py tests/test_terminal_interactive_attach.py tests/test_terminal_teardown_failure.py tests/test_terminal_contract_edges.py tests/test_bridge_tui_pty.py tests/test_scheduler_timeout_review.py -q -o addopts='' --basetemp=/tmp/lb-final-review-terminal-fixed
```

Additionally, the reviewer independently repeated the exact attached-input and
already-dead-server pair **12 consecutive times**, each **2 passed**, with fresh
`/tmp/lb-final-review-terminal-fixed-repeat-{1..12}` basetemps. The repeat
command used the two node IDs `tests/test_terminal_attached.py::test_attached_client_input_paste_mouse_resize_detach_and_reattach` and
`tests/test_terminal_teardown_failure.py::test_stop_removes_metadata_for_an_already_dead_owned_server`,
`-q -o addopts=''`. Runs took 2.00–2.26s each. The implementor separately
reported 12 paired-file passes on each supported Python version; those are
additional implementor evidence, distinct from the reviewer's own execution.

## Final source-review disposition

A1–A24 are closed with their recorded scope and evidence. No unresolved Critical
or High finding remains in the reviewed final implementation. Chronology,
parser/REPL responses, owned native session discovery/restoration, process and
terminal lifecycle, truthful asynchronous monitoring and bounded behavior have
substantive independent review and targeted verification. The original
`docs/features/llm-bridge/prompt.md` remains unchanged.

Source review is approved **conditional on** the root-owned final frozen
whole-suite/global85/Bridge90 gates on Python3.11/3.15 and final CI succeeding
for these exact changes. This is not a claim that those in-flight gates passed.
Live capabilities remain explicitly bounded: Codex0.164.0 reply/exact-ID
resume/context recall and tested native input/wheel passed; provider click
selection is unverified; Claude/Copilot paid completion/resume, physical reboot
and multiweek live execution are unverified. Root owns final gate, publication
and release actions.


Reviewer execution environment: the independent commands above used the local
default **Python3.14.4**. Root's required Python3.11 and Python3.15 final gates
are separate evidence and are not claimed as independently executed by this
reviewer.


### A25 — Medium: real-curses stdin test assumes PTY closure proves process exit — CLOSED, final patch gate pending

- **Location:** `tests/test_bridge_long_prompt_editor.py`, stdin/control exit
  assertion. The post-merge Python 3.11 CI run observed `poll() is None` after
  the PTY-read loop ended; the release tag matrix separately passed.
- The original test breaks on PTY read `OSError` and immediately requires
  `process.poll()==0`. PTY slave closure does not establish that final child
  process exit is observable yet. It also waits for command submission rather
  than the actual stdin-request rejection before beginning its exit-key loop.
- Independent unchanged-node reproduction: **30 consecutive passes** on
  Python 3.11.14, with unique `/tmp/lb-postmerge-review-original-{1..30}`
  basetemps. A disposable instrumented copy showed errno5/EIO at ordinary
  terminal closure in **20/20** runs; all those samples already had exit 0. The
  exact CI failure was not instrumented and its timing is not retrospectively
  proven.
- An independent disposable child explicitly closed descriptors 0/1/2 before
  0.25s of finalization. The PTY master returned EIO while `poll()` was None;
  bounded `wait()` then returned 0 after 0.264s. This establishes the invalid
  lifecycle assumption without attributing a production stdin-capture bug.
- The repaired test waits for the actual `press Ctrl-E` rejection, asserts the
  durable task count remains one, sends one Ctrl-D and awaits the child itself.
  This strengthens the observable contract and removes the shutdown-poll race.
  Independent updated-file runs passed **2 tests in 2.73s on Python 3.11.14** and
  **2 tests in 2.74s on Python 3.15.1**, using separate
  `/tmp/lb-review-a25-fixed-{311,315}` basetemps.
- No production defect was demonstrated. The repair is test-only. Final CI and
  release gates for the separately authorized 1.1.1 launcher/theme work remain
  pending and are not implied by this targeted closure.


### A26 — Medium: semantic-color refactor collapses embedded dashboard columns — CLOSED

- **Location:** `bridge_tui._draw_full` Usage branch and response-overlay
  writer, now routed through `_put` by the 1.1.1 theme refactor.
- `_put` uses `_text`, which intentionally collapses whitespace for ordinary
  labels. The prior Usage writer preserved repeated spaces so the full
  llm-usage renderer's table retained its column alignment. Routing those lines
  through `_put` converts `Provider       Scope       Remaining` into
  `Provider Scope Remaining`. Response indentation is similarly lost.
- An independent actual `_draw_full` call with a controlled Usage snapshot
  confirmed both header and provider rows lose their spacing. This violates
  the accepted full embedded-dashboard contract even though labels remain
  visible and the initial frozen **141-test / 14.14s** changed-surface gate passed.
- Add a whitespace-preserving, control-sanitized write path for already-formatted
  Usage and response lines while retaining semantic attributes and bounded
  width. Regression assertions must check the actual rendered repeated spaces
  and response indentation, not only label presence or row builders.
- Parent and theme implementor were notified during the root coverage freeze;
  the reviewer has not edited production or tests. This blocks approval of the
  new theme patch until repaired and independently verified.


### A27 — Medium: launcher objective can be reinterpreted as a CLI option — CLOSED

- **Location:** `bridge_launcher.run_launcher`, shared mission-create argv.
- A literal objective such as `--help` is valid nonempty form input, but the
  launcher passes it as the argument following a separate `--prompt` token.
  Argparse interprets the objective as an option and raises `SystemExit(2)`
  before mission creation. Because dispatch runs under captured stderr, this
  bypasses the normal form error/retry path and can terminate the launcher
  without an actionable visible error.
- Independent reproduction validated `Launcher(objective='--help')`, then
  invoked the exact shared `main` argv shape: it raised SystemExit2. No mission
  or provider process was created. This is an argument-boundary issue, not
  shell execution; the launcher correctly uses an argv list.
- Pass the objective as a literal option value using `--prompt=<objective>` and
  add a shared-handler regression that stores a leading-dash objective verbatim.
  Parent was notified during the frozen diagnostic run. No production/test
  edits were made by the reviewer.


**A26/A27 independent closure, 2026-10-27:** The styled writer now has a bounded
whitespace-preserving path that still replaces control characters. Usage rows,
response indentation and the navigation background use it. The selected-tab
overlay and mouse layout now share the actual rendered separator spacing.
Assertions cover exact rendered columns, indentation and highlight offset. The
launcher supplies `--prompt=<objective>` and its shared-handler regression stores
`--help` verbatim. The reviewer independently reran the actual Usage and
navigation reproductions; both pass.

The independent post-fix command passed **145 tests in 14.10s on Python 3.14.4**:

```text
python -m pytest tests/test_bridge_launcher.py tests/test_bridge_colors.py tests/test_bridge_long_prompt_editor.py tests/test_bridge_tui.py tests/test_bridge_tui_error_contracts.py tests/test_bridge_tui_projection_contracts.py tests/test_bridge_cli.py tests/test_terminal_backend.py tests/test_terminal_attached.py tests/test_terminal_interactive_attach.py tests/test_terminal_teardown_failure.py -q -o addopts='' --basetemp=/tmp/lb-review-111-final
```

Independent launcher/color/editor gates also passed **39 tests in 3.78s on
Python 3.11.14** and **39 tests in 3.82s on Python 3.15.1**, using the three first
files above with `/tmp/lb-review-111-final-{311,315}` basetemps. These commands
precede the separately identified limited-color bold adjustment; they are not
represented as final whole-suite evidence for that later change.

The reviewer also exercised the actual installed
`~/.local/bin/llm-bridge` in a real PTY from an unrelated temporary
folder, with PYTHONPATH/PYTHONHOME removed and HOME/XDG/config/state isolated.
Both dark and light launches created an objective of `--help` verbatim in the
chosen folder, remained CREATED with zero operations, and exited 0 after one
Ctrl-D. This validates the local installed entrypoint independently of module
tests that deliberately add the repository import path.

The actual dark/light Xvfb control screenshots were independently viewed. The
first limited-color light screenshot exposes bold promotion of the warning
color; root is addressing that narrow rendering issue and retaking the main
screens with explicit 256-color capability. Source review found no further
Critical/High issue. Final 1.1.1 approval awaits that follow-up, the final frozen
whole-suite/coverage gates and exact-head CI; capability-specific native limits
remain as previously recorded.


## Final 1.1.1 independent source-review disposition

The complete startup/theme delta and its follow-up were independently reviewed.
A25 is a test-only shutdown assertion repair; A26 and A27 are closed with
actual rendered-spacing, navigation-coordinate and literal-objective
regressions. The later light-theme eight-color adjustment suppresses bold only
for colored foregrounds, avoiding bright-slot promotion; selection keeps its
bold/reverse cue and dark-mode emphasis remains. The shared SGR override decoder
also honors reset and underline-off semantics.

On the final frozen production/test source, the same eleven-file command above
passed **145 tests in 14.08s on Python 3.14.4**, with
`--basetemp=/tmp/lb-review-111-release-final`. The launcher/color/editor command
passed **39 tests in 3.87s on Python 3.15.1**, with
`--basetemp=/tmp/lb-review-111-release-final-315`. The earlier supported-minimum
39-test gate passed on Python 3.11.14; root separately owns the final full
3.11/3.15 matrix and coverage runs for this last attribute adjustment. These
overlapping gate counts must not be summed as unique tests.

The reviewer viewed the retaken actual installed-control screenshots
`docs/img/llm-bridge-dark.png` and `docs/img/llm-bridge-light.png`, captured through
a real Xvfb/xterm with explicit 256-color capability. Navigation/selection,
warning contrast and dark/light readability have no visual blocker. The capture
mission's CREATED state and zero provider operations do not claim live model
execution. The separate installed-entrypoint checks from an unrelated folder
retain their precise evidence above.

**Source review: approved.** No unresolved Critical, High or Medium finding
remains in the reviewed 1.1.1 changes. Publication approval remains conditional
on the exact final-source Python 3.11/3.15 whole-suite/global85/Bridge90 gates,
artifact installation/version checks and exact-head GitHub CI. Pending runs
are not labelled passed. Native provider capability limits remain unchanged
unless the root-owned latest live probes supply stronger evidence.

Only review records were edited by this reviewer. Production/tests remained
frozen during verification. `git diff --check` passed and the original user
directive remains untouched.
