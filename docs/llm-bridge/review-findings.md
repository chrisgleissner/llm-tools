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

### O5 — High: active task cancellation hides the still-running provider — OPEN

- **Location:** `llm_tools/bridge.py` (`task.cancel`) and the owned provider
  operation.
- **Reproduction:** Start an operation and apply `task.cancel`. The command
  result contains `status=CANCELLED` but no `work_status=DEFERRED`; the
  operation remains RUNNING and the provider pane is not signalled. The task
  state stays CANCELLED after the provider eventually returns.
- **Impact:** The operator cannot tell from the command result that the
  provider may continue mutating the worktree after cancellation was requested.
- **Required fix:** Either stop the owned provider through the safe process
  ownership path, or return an explicit deferred outcome like active
  `task.pause` and define when the cancellation takes effect.
- **Verification:** Add an active-task cancellation command test asserting the
  provider is stopped or the result clearly reports deferred work and its
  operation remains tracked until exit.

## Gate 4 — Ralph hook integration

### O3 — High: a stop racing pre-launch is misreported as controller failure — OPEN

- **Location:** `llm_tools/ralph_robin.py` (`main`) and
  `llm_tools/bridge_runtime.py` (`BridgeMissionHooks.before_launch`).
- **Fixes:** Ralph now skips the scheduler when `before_launch()` returns False.
  `begin_operation()` checks mission RUNNING in its transaction, and the hook
  treats a rejected intent after a concurrent mission stop as a normal launch
  rejection. `test_before_launch_treats_concurrent_mission_stop_as_rejected_launch`
  plus `tests/test_bridge_hooks.py` passed (3 tests).
- **Current reproduction:** A task can be cancelled after selection but before
  `begin_operation()`. The store correctly rejects `task is not runnable`, but
  mission status remains RUNNING, so `before_launch()` re-raises the
  `TransitionError`; the controller then treats it as failure.
- **Impact:** A valid concurrent task-cancel command can fail the whole mission
  even though the provider was correctly prevented from launching.
- **Required fix:** Treat the operation-intent rejection as a normal launch
  rejection when the task is no longer runnable as well as when the mission is
  no longer RUNNING. Keep unexpected store errors visible.
- **Verification:** Add a task-cancel interleaving regression proving no
  operation/provider launch occurs and the controller does not become FAILED.

### O4 — High: stop requests are not polled during Ralph/scheduler waits — OPEN

- **Location:** `llm_tools/ralph_robin.py` (`suspend_until_available`,
  `suspend_machine_until`) and `llm_tools/scheduler.py`
  (`wait_until_usable`).
- **Evidence:** `BridgeMissionHooks.before_iteration()` is the only runtime
  hook that reads mission lifecycle status. Ralph calls its provider wait and
  suspend paths without a Bridge callback, and scheduler readiness can sleep
  until a reset without checking mission state.
- **Impact:** A graceful stop during an all-rate-limited wait or scheduler
  readiness wait is not observed until that wait returns; in the suspend path
  that can be the next provider reset.
- **Required fix:** Thread a bounded stop/pause check through long waits and
  suspend wake/resume boundaries, without changing default no-hook behavior of
  ordinary Ralph/scheduler runs.
- **Verification:** Add deterministic fake-clock/fake-suspend tests that issue
  STOPPING during each wait path and prove the loop exits promptly without
  launching another provider. Do not perform real machine suspend in CI.

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

### O7 — High: graceful stop leaves the private terminal server running — OPEN

- **Location:** `llm_tools/bridge_runtime.py` (`run_controller` shutdown) and
  `llm_tools/bridge.py` (`stop` command).
- **Evidence:** Graceful `stop` only records STOPPING. After Ralph exits,
  `run_controller()` changes STOPPING to STOPPED but does not stop or detach
  `backend`/`handle`. The explicit server cleanup exists only in the
  `stop --force` path.
- **Impact:** The tmux server and Bridge console pane remain alive after
  graceful stop; an attached `llm-bridge start` caller remains in tmux instead
  of returning to its terminal.
- **Required fix:** After the active operation reaches its safe boundary,
  clean up the owned terminal station and restore attached clients before
  marking STOPPED. Preserve detach semantics separately.
- **Verification:** Add an attached real-PTY stop test: stop during an active
  fake operation, wait for its boundary, assert the invoking client returns,
  the private server/socket/config are removed, and mission state is STOPPED.

### O8 — High: resume reuses a live pane after marking its operation uncertain — OPEN

- **Location:** `llm_tools/bridge_runtime.py` (`run_controller` recovery and
  `_open_existing`).
- **Evidence:** Startup reconciles every PREPARED/RUNNING operation to
  UNCERTAIN/BLOCKED, then opens an existing private tmux session without
  checking provider pane liveness or terminating/verifying its owner. Ralph
  waits because the task is blocked while the old provider pane can remain
  active.
- **Impact:** A provider may continue writing after the new controller has
  marked its operation uncertain and reported the mission resumed. The
  reconstructed task state and observed station state can contradict each
  other.
- **Required fix:** On recovery, inspect the persisted operation and owned
  station identity before reusing a pane. Either verify and explicitly restore
  the native session, or safely terminate the owned process before rebuilding
  context; if identity is unavailable, keep the mission in an explicit recovery
  state and do not claim a clean resume.
- **Verification:** Add a real-tmux fake-provider test that kills only the
  controller while a provider remains alive, then resumes. Assert the provider
  is verified as restored or terminated before the new controller becomes
  ready, with the operation remaining UNCERTAIN and never silently replayed.

### O9 — High: native provider restoration has no persisted session identity — OPEN

- **Location:** `llm_tools/bridge_store.py` operation schema,
  `llm_tools/bridge_runtime.py` (`BridgeMissionHooks.before_iteration`), and
  station/status projection.
- **Evidence:** Operations persist route/model/pane/process identity but no
  provider conversation/session id. The runtime always labels the next prompt
  `RECONSTRUCTED`; it has no native resume argv path, and `status_snapshot()`
  does not project station restoration state. The capability ledger lists
  resume interfaces for installed providers, while runtime restoration remains
  NOT VERIFIED.
- **Impact:** Resume always rebuilds context and cannot restore a provider-native
  conversation where supported; operators cannot distinguish an observed
  native restore from reconstructed context in station state.
- **Required fix:** Capture a typed, provider-specific session id only from
  structured, validated CLI metadata and use the exact documented resume
  interface when verified. Otherwise expose RECONSTRUCTED/UNAVAILABLE and keep
  native restoration explicitly NOT VERIFIED; never use a global most-recent
  session selector.
- **Verification:** Use fake provider CLIs to persist an exact session id,
  verify the resume argv uses only that id, and verify missing/mismatched ids
  fall back to RECONSTRUCTED. Keep real provider restoration NOT VERIFIED until
  separately observed.

## Gate status

- Persistence gate: **CLOSED at the last frozen checkpoint** — P1-P8 and the
  verifier/process-identity additions passed the 94-test suite. The later O3
  hook change has only targeted verification (3 tests).
- Native terminal gate: **CLOSED** — T1-T6 passed at this checkpoint. CI
  configuration was reviewed locally; no CI run has been reviewed.
- Operator command dispatch: **OPEN** — O1 and the status-overwrite part of O2
  are closed; O5 still hides provider work during active task cancellation.
- Ralph hook integration: **OPEN** — O3 is a high-severity stop/launch race.
- Stop and wait lifecycle: **OPEN** — O4 is an unpolled-stop risk during long
  waits.
- Controller recovery: **OPEN** — O6 is closed; O8 still allows an unverified
  live pane after recovery.
- Graceful stop restoration: **OPEN** — O7 leaves the private tmux station alive.
- Process/session recovery: **OPEN** — O8 can reuse a live provider pane after
  marking its operation uncertain.
- Native restoration: **OPEN / NOT VERIFIED** — O9 captures no provider
  session id or native-resume state; current prompt context is reconstructed.
- Provider handoff, session reconstruction and final integrated gates have not
  yet been reviewed here.
- No live provider capability is claimed by this review. Root's capability
  ledger remains authoritative and reports model execution as NOT VERIFIED.
