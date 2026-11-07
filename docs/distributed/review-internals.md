# Python Bridge internals review (PRD-034)

Scope: `llm_tools/bridge_runtime.py`, `llm_tools/bridge_store.py`, the controller start, stop and `stop --force` paths in `llm_tools/bridge.py`, `llm_tools/bridge_worker.py`, the scheduler watchdogs as Bridge uses them (`llm_tools/scheduler.py`, `llm_tools/common.py`), and `llm_tools/bridge_missions.py`. Bridge-adjacent code that the controller calls (`bridge_logs.py`, `bridge_refresh.py`, `bridge_commands.py`, `ralph_robin.py`) was read where a finding depends on it.

Method: read-only code review. Findings marked "reproduced" were confirmed with small throwaway scripts that use a temporary Bridge state directory (`/home/chris/llm-tools-infra/prd-py.sh`, no CLI mission was created in the real state). The scripts are not part of the repository. Findings marked "by reading" were verified from the code paths only. Anything not confirmed is labelled "unverified". No code was changed.

## Summary

| Severity | Count |
| --- | --- |
| critical | 0 |
| high | 5 |
| medium | 8 |
| low | 6 |
| total | 19 |

Most important problems:

1. INT-001: `BridgeMissionHooks.before_iteration` never returns when no task is ready and the tasks are not all COMPLETED. A task that ended BLOCKED, FAILED or CANCELLED (or waits on such a task) keeps the controller in a 0.5 s loop with no event, no log line and no exit. A mission with one COMPLETED and one CANCELLED task can never complete. This is the known hang (concern 2).
2. INT-004: the scheduler question watchdog treats any line that starts with a question or choice word and ends with `?`, anywhere in the last eight non-empty lines, as a pending prompt. It scans stdout and stderr as one stream, and it aborts with status 75 after 30 s of silence. Status 75 is never retried by Bridge, so the task is BLOCKED permanently and INT-001 then spins. The specific Python `SyntaxWarning` shape from concern 1 does not match the current regex (see INT-004), but the mechanism is confirmed with other inputs.
3. INT-002: when the controller exits through an exception, nothing terminates the provider process group. The mission becomes FAILED, which releases the worktree, while the provider can keep editing it.
4. INT-003: the two "refuse to start replacement work" recovery guards (controller start and `stop --force`) run after the operations were already marked UNCERTAIN. A second attempt no longer sees those operations, so the guard protects only the first attempt. The same code also treats a reused PID as a reason to refuse, although a changed start token proves that the recorded process is gone.
5. INT-005 and INT-006: crash reconciliation overwrites an operator's CANCELLED or PAUSED task with BLOCKED, and `llm-bridge stop` on a RUNNING mission whose controller is dead reports success but leaves the mission in STOPPING indefinitely.

Open items and limits of this review:

- Concern 1 is only partly reproduced. A path-prefixed Python `SyntaxWarning` line does not match `QUESTION_LINE_RE`. Whether a real provider in the field emitted a different shape that matched was not determined.
- INT-009 (exec failure in the `pty.fork` child) is reproduced up to the point where the child continues running caller code. The downstream effect on a real controller was not exercised.
- INT-002, INT-008, INT-010 and INT-012 are by reading only.
- Whether a native-mode operation records `process_pid` is not verified (INT-003, last paragraph).
- Not reviewed: `bridge_power.py`, `bridge_terminal_stream.py`, `bridge_protocol.py`, `bridge_ws.py`, `terminal.py`, the remainder of `scheduler.py` (rate-limit retry, wake scheduling), and the Rust coordinator.
- The worker service (`bridge_worker.py`) showed no correctness or security defect beyond INT-013. Its token and CA files are written 0600 through `_atomic_private_write`, the audit log is bounded and avoids secrets, and generation numbers are persisted before any command of that generation is accepted.

Areas that were checked and look sound: SQLite configuration (WAL, `synchronous=FULL`, `busy_timeout` of 10 s on both writer and read-only connections, `BEGIN IMMEDIATE` for writes, savepoints for nesting, schema migration only when `user_version` is behind), private-directory ownership checks on the state, lock and runtime directories, process identity tokens (`boot_id:starttime` from `/proc/<pid>/stat`, parsed after the last `)`), verifier output caps (10 MiB log, 1 MiB captured), verifier artifact path containment after `resolve(strict=True)`, and the no-follow digest of provider logs.

## Findings

### INT-001 (high): controller loops forever when no task can run and not all tasks are COMPLETED

Reproduced.

Location: `llm_tools/bridge_runtime.py:355-369` (inside `before_iteration`, which starts at line 281), together with `llm_tools/bridge_store.py:716-722`.

Code path: `before_iteration` loops. When the mission is RUNNING, no review hold applies and `ready_tasks` returns nothing, it counts tasks by status. It returns `stop` only if every counted task is COMPLETED (line 366). In every other case it executes `time.sleep(0.5)` (line 369) and repeats.

Failure scenarios (inputs and state, then outcome):

- Every task is BLOCKED. Status 75 (autonomy abort), the declined trust prompt (76) and an interrupted operation (UNCERTAIN, set by `reconcile_interrupted_operations`) all produce BLOCKED. Nothing moves a BLOCKED task back to READY except an operator action or `release_declined_tasks` at the next start. The controller spins, the mission stays RUNNING, and `one_active_mission_per_worktree` keeps the worktree reserved. Reproduced: two BLOCKED tasks produced six consecutive `sleep(0.5)` calls and no return value, and no event rows were written after the mission start.
- One task COMPLETED and one CANCELLED. The completeness test counts CANCELLED tasks, so `COMPLETED == total` is false. The loop never ends, and `set_mission_status(..., "COMPLETED")` would also refuse (`bridge_store.py:719-721`, it compares total with completed). Reproduced the same way.
- A FAILED task, or a QUEUED task that depends on a BLOCKED, FAILED or CANCELLED task, also produces an endless wait.
- VERIFYING tasks with a failed configured verifier remain VERIFYING (`record_verification_attempt`). In headless or auto-continue missions no review hold applies, so the same loop waits for an operator who has no signal that a decision is needed.

Consequences: the process never exits, so any test that drives a controller to completion hangs (a full-suite run hung for 25 minutes on this). The operator sees a RUNNING mission with no event that says why nothing happens. Each pass takes the SQLite write lock (see INT-013).

Suggested fix:

- Decide the intended meaning of "no runnable task". Recommended: when no task is READY, QUEUED-with-met-dependencies, RUNNING or VERIFYING-under-hold, compute a terminal verdict. If the effective tasks are all COMPLETED or CANCELLED and at least one is COMPLETED, set COMPLETED. If all are CANCELLED, stop. Otherwise record one `mission-waiting-for-operator` event (reason, task ordinals and statuses, and which command unblocks them) and move to a longer, backed-off wait, or stop the controller with a recorded reason so that `start` resumes it. Treat CANCELLED like COMPLETED in the completeness test (`FINISHED_TASK_STATUSES` already exists).
- Make the wait event-driven or back off to a few seconds, and add an optional `idle_exit_seconds` setting so tests and unattended runs can bound it.
- Add a regression test that runs `before_iteration` against a mission with BLOCKED tasks and asserts that it returns within a bound or emits the waiting event.

### INT-002 (high): provider process group survives a controller exception while the mission becomes FAILED

By reading.

Location: `llm_tools/bridge_runtime.py:2107-2116` (the `except BaseException` handler) and the `finally` block that follows; process creation in `llm_tools/scheduler.py` (`_popen_with_prompt`, `start_new_session=True`) and `llm_tools/common.py:4067` (`pty.fork()`, whose child is a session leader).

Failure scenario: the provider runs headless in its own session. An exception in the controller thread (for example `sqlite3.OperationalError: database is locked` after the 10 s busy timeout, a `ValueError` from `record_runtime_event`'s 16 KiB limit, `OSError` from the log directory, or INT-012) unwinds through `ralph_robin.main`. The handler calls `reconcile_interrupted_operations` (operation becomes UNCERTAIN, task BLOCKED), records `controller-failed`, and sets the mission FAILED. It does not call `_terminate_owned_process_group` for the operation's persisted `process_pid`. FAILED is not one of the statuses covered by `one_active_mission_per_worktree` (`RUNNING`, `PAUSED`, `STOPPING`), so another mission can be created in the same worktree while the old provider still runs. The orphan's output and exit code are lost.

Cleanup only happens at the next controller start (the recovery block at `bridge_runtime.py:1977-2016`), and only for the first attempt (see INT-003).

Suggested fix: in the exception handler, before `reconcile_interrupted_operations`, terminate the process groups of operations in INTENT, PREPARED or RUNNING that have a persisted identity (`terminate_managed_processes` plus `_terminate_owned_process_group`, as `stop --force` does). Keep the mission out of FAILED until that termination succeeds, or record a `provider-orphaned` event with the pid and start token so recovery can find it.

### INT-003 (high): recovery refusal is single-shot, and PID reuse is treated as danger

By reading.

Locations: `llm_tools/bridge_runtime.py:1977-2016` (controller start recovery), `llm_tools/bridge.py:2447-2489` (`stop --force`).

Failure scenario A (controller start): the operation list `interrupted` is read at line 1977. `reconcile_interrupted_operations` runs at line 1987 and sets those operations UNCERTAIN. Only afterwards does the loop at lines 1999-2016 decide whether it can prove the identity of a surviving provider. If it cannot (a RUNNING operation without `process_pid` or `process_start`, headless or without a station handle), it raises `RuntimeError("... refusing to start replacement work")`. The exception leaves the mission status unchanged. On the next `start`, the SELECT at line 1977 no longer returns the operation (status is UNCERTAIN), so recovery finds nothing to check and starts a replacement run in the same worktree. The refusal therefore works once. A controller that crashed in the short window between starting the provider and `record_operation_process` is the realistic trigger.

Failure scenario B (`stop --force`): `bridge.py:2463` calls `reconcile_interrupted_operations` before any process is signalled. If the later loop raises `RuntimeError("no verifiable process identity; mission remains STOPPING")` (line 2489) or `_terminate_owned_process_group` raises, a second `stop --force` finds `operations == []` and never signals the provider. The saved `process_pid` and `process_start` stay in the UNCERTAIN row, but no code path reads UNCERTAIN rows for termination except `bridge_power` bookkeeping.

Failure scenario C (reuse treated as a hazard): lines 2003-2008 raise `PermissionError("persisted provider pid ... now belongs to a different process; refusing recovery cleanup")` when the recorded start token differs from the token now at that PID. The token is `boot_id:starttime`. A different token means the recorded process no longer exists, so there is nothing to terminate and nothing to refuse. After a reboot or PID wraparound this aborts controller start (and, via scenario A, the abort is consumed by the first attempt). `stop --force` has the same shape for the controller itself at `bridge.py:2505-2506`: after a reboot, if any unrelated process now holds the old controller PID, `stop --force` raises "controller PID no longer matches" and leaves the mission STOPPING, although the lock is free and the old controller is certainly dead.

Related, unverified: in native mode, a RUNNING operation is created with `pane_id` and may have no `process_pid` (`mark_operation_running` in `before_launch` passes none; `_record_process` fills it only when the scheduler reports a pid). After a reboot the tmux server is gone, `prior_handle` is `None`, and the same `RuntimeError` fires on the first start attempt, although no provider can be alive. I did not confirm whether the tmux execution mode always reports a pid.

Suggested fix:

- Terminate and verify first, then reconcile. Perform the identity checks and terminations before `reconcile_interrupted_operations`, or keep a durable per-operation "needs process check" marker until the check succeeds, and make the recovery SELECT include UNCERTAIN operations whose marker is set.
- Treat a changed or absent start token as "already gone" (skip), and refuse only when the identity cannot be read at all.
- In `stop --force`, if the mission lock is free, skip controller termination instead of comparing identities.

### INT-004 (high): question watchdog aborts healthy runs with status 75, and Bridge never retries 75

Mechanism reproduced; the SyntaxWarning shape from concern 1 not reproduced.

Locations: `llm_tools/scheduler.py:1251-1291` (`ProgressGuard.note_output` and `overdue`), `llm_tools/scheduler.py:1416` and `1605` (stderr chunks scanned), `llm_tools/common.py:3994` and `4030-4033` (`BLOCKING_PROMPT_TAIL_LINES = 8`, `QUESTION_LINE_RE`), `llm_tools/scheduler.py:1009-1010` (comment on the tool-activity marker), `llm_tools/bridge_runtime.py:936` (no retry for 75).

Findings, each checked by running `ProgressGuard` on sample text:

1. Stderr is not separated from stdout. Both streams go through the same scan buffer (`scan_tail(chunk)` at line 1416 for stderr in the Claude stream path, `scan_prompt(chunk)` at lines 1568 and 1605 in the generic headless path). A stderr line that matches `QUESTION_LINE_RE` arms the watchdog exactly as model output does.
2. The comment in `note_output` says a question counts only when "the most recent output still ends in a question". The code searches the whole eight-line tail (`common.blocking_tail`, then `QUESTION_LINE_RE.search(combined_tail[-4000:])` at line 1275). Reproduced: `"Which approach should I take?\nI'll go with A and run the tests.\n[tool activity]\n"` leaves `question_seen_at` set. A question followed by seven further non-empty lines still counts; it clears only when eight more lines push it out of the tail.
3. `question_seen_at` is reset to the current time on every output chunk while the question stays within the tail, so the 30 s timeout measures silence after the last output. Model text that contains a question, followed by a tool call or long computation that prints nothing for more than 30 s, aborts the run with `autonomous abort: question required a response for 30s` and status 75. The tool-activity marker documented at lines 1007-1009 as ending a pending question does not do so (reproduced).
4. Concern 1 specifically: a Python `SyntaxWarning` such as `<f>:1: SyntaxWarning: "is" with 'int' literal. Did you mean "=="?` does not match, because the regex requires the line to begin with a question or choice word (`what`, `which`, `select`, `choose`, `confirm`, `continue`, `proceed`, and similar) after optional list punctuation. I ran the real Python 3.12 warning text through `ProgressGuard.note_output` and it stayed idle. The same path does flag a stderr line such as `Continue without the lockfile?`. In addition, `BLOCKING_PROMPT_PATTERNS` (`common.py:4010-4027`) abort immediately, with no quiet period, if any of the last eight lines of merged output contains phrases like `monthly spend limit`, `esc to cancel` or `out of credits`, including phrases that appear in repository text or tool output on the raw-output path (`renderer is None`).
5. Bridge treats status 75 as final for the operation: `bridge_runtime.py:936` sets `continued = None` for `AUTONOMY_ABORT_STATUS`, and `finish_operation` maps 75 to BLOCKED. The task stays BLOCKED until an operator acts, and with one task the controller then spins (INT-001).

Suggested fix: scan stdout and stderr separately and ignore stderr for question detection. Require the question to be the last non-empty line of model-authored text, and clear it on any later chunk of any stream, including tool activity. Raise the quiet period for question detection above the longest tool silence that is normal for the provider, or restrict the check to providers and modes where a prompt can actually appear (attached TUI). Make 75 an event that records the matched line (bounded) so false positives can be diagnosed.

### INT-005 (medium): crash reconciliation overwrites CANCELLED and PAUSED tasks

Reproduced.

Location: `llm_tools/bridge_store.py:1223-1235` (`reconcile_interrupted_operations`, the UPDATE at line 1232).

Scenario: the operator cancels or pauses a task whose operation is still RUNNING. `finish_operation` is designed to honour that (`boundary_status`), but if the controller crashes or `stop --force` runs first, `reconcile_interrupted_operations` executes `UPDATE tasks SET status='BLOCKED'` for every task with an unresolved operation, without checking the current status or `TASK_TRANSITIONS`. Reproduced: task CANCELLED with a RUNNING operation became BLOCKED after reconcile and could then be moved to QUEUED. CANCELLED has no outgoing transitions in `bridge_states.TASK_TRANSITIONS`, so this violates the model.

Suggested fix: only change tasks whose status is RUNNING (or QUEUED or READY for a PREPARED operation); leave CANCELLED and PAUSED as they are, and record the operation as UNCERTAIN regardless.

### INT-006 (medium): `stop` on a RUNNING mission with no controller reports success and leaves STOPPING

Reproduced through the CLI against a temporary state directory.

Location: `llm_tools/bridge.py:342-351` (`_apply_operator_effect`, action `stop`).

Scenario: a controller died and left the mission RUNNING. `llm-bridge stop` takes the branch `mission["status"] in STARTED_MISSION_STATUSES` and sets STOPPING. The no-op test above it only covers CREATED, STOPPED and PAUSED-without-controller. Nobody is left to finalize STOPPING to STOPPED. Output: `Stop requested · mission 1bb46af0 is STOPPING`; `status` then shows `STOPPING (background)` with `Controller unknown`. The state also keeps counting as active for `one_active_mission_per_worktree`. `start` on a STOPPING mission only completes the stop (`run_controller` lines around 2022-2027 return after setting STOPPED), so recovery takes two commands and the first message is misleading.

Suggested fix: when the status is RUNNING or PAUSED and `controller_running` is False, move straight to STOPPED (after the same station and process cleanup as `stop --force`), or say that the controller is gone and point at `stop --force`.

### INT-007 (medium): failures in `run_controller` before the main `try` leave the mission RUNNING with no failure record

By reading.

Location: `llm_tools/bridge_runtime.py:2020-2075` (status change to RUNNING, `rotation_problem`, `_open_existing`, `backend.start`) versus the `try` that starts at the `refresh_worker` assignment.

Scenario: after `set_mission_status(mission_id, "RUNNING")` (line 2021), an exception from `_install_frozen_tool_config`, `backend.start` (tmux fails to create the server or socket), `_open_existing` or `set_rotation` escapes `run_controller` through the `with` statements. Only the tmux-missing branch calls `record_controller_failure` and sets FAILED. The mission stays RUNNING with no controller and no `controller-failed` event. `launch_controller` then reports "mission controller exited with status 1" and `_recorded_failure` returns an empty string. The stale RUNNING mission holds the worktree until the operator runs `stop --force` or `start`.

Suggested fix: move the post-RUNNING setup inside the existing exception handler, or wrap it so that any exception records `controller-failed` and sets FAILED before propagating.

### INT-008 (medium): the controller log sink has no error handling and its drain timeout can replace the exit code

By reading.

Location: `llm_tools/bridge_logs.py:72-105` (`capture_process_output`), used by `llm_tools/bridge.py:2175-2188` around `run_controller`.

Scenario 1: the reader thread `consume` has only a `try/finally`. If `_append_rotating` raises `OSError` (disk full, permission change, log directory removed), the thread exits and closes `read_fd`. Descriptors 1 and 2 of the controller still point at the pipe's write end. The next `print` or `status_line` call in the controller raises `BrokenPipeError` (Python ignores SIGPIPE), at an arbitrary point in the Ralph loop, which becomes an exception for INT-002.
Scenario 2: on exit, `reader.join(timeout=5)` waits for EOF on the pipe. Any descendant that inherited descriptors 1 or 2 and outlives the controller (a dev server started by a provider, a daemonized tool) keeps the write end open. The `finally` raises `TimeoutError`, which replaces the return value of `run_controller`, so the exit status and the final status handling are lost to a traceback.

Suggested fix: catch `OSError` inside `consume`, keep draining the pipe (discarding data) and log the failure once; on drain timeout, log and return normally, since the thread is a daemon.

### INT-009 (medium): `pty.fork` child continues as a clone of the caller when `exec` fails

Reproduced (child survives); downstream effect in a real controller not exercised.

Location: `llm_tools/common.py:4067-4082` (`run_pty_capture`).

Scenario: in the child branch, `os.execvp` or `os.execvpe` raises (`FileNotFoundError`, `PermissionError`, `OSError: Argument list too long`, a bad interpreter). Nothing catches it and nothing calls `os._exit`, so the exception propagates through the caller in the forked process. Reproduced with `run_pty_capture(["/nonexistent/provider-cli"], ...)` wrapped in `try/except` and `os._exit(0)` in the child: the clone ran the caller's code, and the parent reported status 0 for a program that does not exist. In the real call chain (`scheduler.run_fresh_headless`, `common.read_copilot_live`, whose `except Exception` returns `capture-error` inside the clone) the clone would continue as a second controller or usage reader that shares the parent's SQLite connection and the mission lock's open file description until it exits. The controller process is also multi-threaded at this point (the capacity refresh thread), which makes `fork` itself unsafe for anything beyond an immediate `exec`; Python 3.12 warns about this.

Suggested fix: wrap the child branch in `try: ... except BaseException: os._exit(127)`, and prefer `subprocess.Popen` with a pty pair (`start_new_session=True`) instead of `pty.fork` in a threaded process.

### INT-010 (medium): `controller-ready.json` is the only record of controller identity; launch can delete it or start two controllers

By reading.

Locations: `llm_tools/bridge_runtime.py:2187-2223` (`launch_controller`: acquire the lock, `lock.release()` at line 2207, then `ready.unlink()` at line 2223, then spawn), `llm_tools/bridge_runtime.py:1947-1955` (`run_controller` also unlinks), `llm_tools/bridge.py:2494-2509` (`stop --force` reads the pid and start token from the ready file).

Scenario 1: two `start` commands for one mission run at the same time. Both probe the lock successfully because each releases it immediately after the probe (line 2207), before either controller exists. Both spawn a controller. One controller acquires the lock and writes the ready file. The other launcher's `ready.unlink()` may run after that write, deleting the winner's identity record. The winner's launcher then waits 15 s and reports "did not become ready" although the controller is healthy, and a later `stop --force` reads no pid, skips terminating the controller, and fails after 3 s with "controller still owns the mission lock; mission remains STOPPING". The losing controller exits with status 2 after its 0.5 s lock wait, and its launcher reports a failure.
Scenario 2: the file may be absent or corrupt for other reasons (partial cleanup, manual deletion). The lock holder's identity cannot be recovered, because flock does not record a pid.

Suggested fix: keep the lock held across the spawn (pass the lock file descriptor to the controller, or have the launcher create a short-lived start lock distinct from the lifetime lock), and write the owner's pid and start token into the lock file while holding it, so `stop --force` can read the identity from the lock itself. Do not unlink the ready file from the launcher after the controller may have written it; use the startup token to disambiguate.

### INT-011 (medium): event and command history grows without a bound except for two event kinds

By reading.

Location: `llm_tools/bridge_store.py:1583-1653` (`prune_plumbing_events` deletes only `controller-heartbeat` and `capacity-refresh`), `llm_tools/bridge_store.py:550-560` (`_event` has no size limit), `llm_tools/bridge_store.py:1011-1023` (`finish_command` writes the whole result into an event), `llm_tools/bridge_runtime.py:189-199` (`on_wait`), `llm_tools/ralph_robin.py:2050-2090`.

Scenarios:

- `provider-wait` and `provider-selection` events are written on every loop pass of a capacity wait with an unknown recovery time. The default poll interval is 60 s, so a long outage writes up to 1,440 `provider-wait` rows per day, each up to 16 KiB (the limit in `record_runtime_event`), and none is pruned.
- `_event` (used by every store transition) has no payload limit, unlike `record_runtime_event`. `finish_command` copies the full command result into the `command-applied` event, and for `task.add` that result contains the task prompt. `add_task` has no prompt size limit (only proposals are capped at 64 KiB), so one large prompt is stored in `tasks`, `commands.payload_json`, `commands.result_json` and the event.
- The `commands` and `operations` tables are never pruned.

Suggested fix: add `provider-wait` and `provider-selection` to the thinning rules (keep the first and last of an unchanged wait), apply a size cap in `_event`, store only a summary of the result in the `command-*` event, and add a retention policy for finished commands and old operations.

### INT-012 (medium): `after_iteration` captures session metadata before it records the provider's exit

By reading.

Location: `llm_tools/bridge_runtime.py:848-855` (`_record_session_outcome` runs before `finish_operation`; only `TransitionError` is caught at line 971).

Scenario: the provider has exited with a known exit code and log. `_record_session_outcome` reads provider session files and resolves the working directory with `Path(...).resolve(strict=True)`. An `OSError`, a `FileNotFoundError` for a removed worktree, or a parse error raised there propagates before `finish_operation` is called. The operation stays RUNNING, the controller dies (INT-002), and recovery marks the operation UNCERTAIN even though the exit status was available. The best-effort session bookkeeping can therefore turn a finished run into an uncertain one.

Suggested fix: call `finish_operation` first, then perform the session capture inside its own `try/except Exception` that records a `native-session-restoration` event with state `UNAVAILABLE`.

### INT-013 (low): idle loops take the SQLite write lock and rewrite file modes continuously

By reading.

Locations: `llm_tools/bridge_runtime.py:281-369` (loop every 0.5 s: `ready_tasks`, which opens `BEGIN IMMEDIATE`; `sync_review_hold_events`; status counts), `llm_tools/bridge_runtime.py:183-188` and `628-642` (`wait_interrupted` calls `ready_tasks(limit=50)` once per second during waits), `llm_tools/bridge_store.py:512-519` and `521-545` (`_secure_sqlite_files` does stat and chmod on three files after every transaction), `llm_tools/bridge_worker.py:433` (`next_events` opens a new `BridgeStore` every 0.5 s through `open_store`, which is not read-only).

Effect: with no work, each controller issues several short write transactions per second, plus about six `stat`/`chmod` calls per transaction. With many idle missions (and a worker) this contends for the single SQLite writer and wakes the disk needlessly. `ready_tasks` also performs promotions (writes and `task-ready` events) when called from what is a read-only check in `wait_interrupted`.

Suggested fix: split `ready_tasks` into a read-only query and a promotion step; poll `PRAGMA data_version` (or the existing redraw probe) before doing any read work; do `_secure_sqlite_files` only after creation or migration; keep one read-only connection in the worker session.

### INT-014 (low): `resume` uses a zero-timeout lock probe that a liveness check can defeat

By reading; not exercised under contention.

Location: `llm_tools/bridge.py:2208` and `2225` (`probe.acquire()` and `lock.acquire()` with the default `timeout=0.0`), compared with `bridge_runtime.py:1879-1882` (`_LOCK_STARTUP_WAIT_SECONDS = 0.5`, with the comment that liveness probes briefly hold the lock).

Scenario: `MissionLock.is_held` takes `LOCK_SH` for a moment. A console, `mission list` or switcher refresh running at that instant makes the `resume` command's `LOCK_EX|LOCK_NB` fail. `resume` takes the "controller already owns the mission" branch, prints the receipt and returns 0. The status was already set to RUNNING by `_apply_operator_command`, so the mission is RUNNING with no controller (leading to INT-006). `is_held` also returns False for any `OSError` other than `BlockingIOError` (for example `ENOLCK`), which reports a possibly live controller as absent.

Suggested fix: use `acquire(timeout=_LOCK_STARTUP_WAIT_SECONDS)` in these two places, and treat an unexpected `OSError` from the probe as unknown.

### INT-015 (low): watchdogs use the wall clock

By reading.

Locations: `llm_tools/scheduler.py:1246` and `1283` (`ProgressGuard.last_progress` and `overdue` use `time.time()`), `llm_tools/common.py:4164-4190` (`run_pty_capture` uses `time.time()` for the idle and question timers).

Scenario: a clock step forward (NTP correction, or the host resuming from suspend while a provider is running) makes `now - last_progress` exceed the 600 s idle or 30 s question limit immediately and aborts a healthy provider with status 75. Bridge's own suspend path only runs between iterations, so the likelihood is low, but the desktop idle suspend described in the project notes can still happen.

Suggested fix: use `time.monotonic()` for the elapsed-time comparisons; keep the wall clock for log timestamps.

### INT-016 (low): verifier runs on the controller's main thread, and heartbeats come from a different thread

By reading.

Locations: `llm_tools/bridge_runtime.py:1166-1334` (`run_configured_verifier` called from `after_iteration` and `_apply_pending_boundary_requests`), `llm_tools/bridge_refresh.py:166-260` (the heartbeat is written by the refresh worker thread).

Effect: a verifier can run for up to 3,600 s (the configured maximum) while the controller does nothing else, so a graceful `stop`, `pause` or `cancel` waits for it. Because `controller-heartbeat` events come from a separate daemon thread, they continue while the main loop is blocked (verifier, INT-001 spin, or a hung call), so a fresh heartbeat does not show that the controller is making progress. Not a defect by itself; it limits what the heartbeat can prove.

Suggested fix: have the main loop update a progress counter that the heartbeat includes, and let `wait_interrupted`-style checks stop the verifier on STOPPING or CANCELLED.

### INT-017 (low): small hardening items in lock and path handling

By reading.

- `llm_tools/bridge_store.py:1913-1915` (`MissionLock.acquire`): if `os.chmod` raises after `self._file` was assigned, the file stays open and `self._file` is set, so a later `acquire()` returns immediately (line 1911) as though the lock were held. The directory is private and owned by the user, so this requires an unusual filesystem condition. Set `self._file` only after the chmod and the successful `flock`, and close the file on any exception.
- `llm_tools/bridge_runtime.py:1337-1341` (`_runtime_directory`) and `llm_tools/bridge_store.py:644-660` (`create_mission(mission_id=...)`) do not validate the mission ID, while `MissionLock` does (`[A-Za-z0-9_.-]+`). No caller in `llm_tools/` passes an explicit ID, so IDs are UUID hex today, but an ID containing `/` or `..` would build a path outside the runtime directory. Apply the same validation in `create_mission`.

### INT-018 (low): `cancel` returns while the provider keeps running

By reading; documented behaviour, listed because of its interaction with INT-005.

Location: `llm_tools/bridge.py:352-362` (`cancel` action) and `llm_tools/bridge_store.py:1183-1221`.

`cancel` marks the mission and all unfinished tasks CANCELLED and returns "cancel is durable; the active provider returns at its boundary". The running headless provider is not signalled, so it can continue for as long as its own run takes (hours) and keep spending quota and editing the worktree of a CANCELLED mission. Only `stop --force` terminates it, and that path then applies INT-005 to the already-cancelled tasks.

Suggested fix: have `cancel` signal the owned process group (the same identity-checked termination that `stop --force` uses) when an operation is active, or print the `stop --force` hint in the cancel receipt.

### INT-019 (high): a provider line that ends in `?` blocks a headless task with no visible reason

Fixed (PRD-034, release blocker).

Description: the shared question classifier (`common.ends_with_question`, used by the headless watchdog in `scheduler.ProgressGuard` and by `common.run_pty_capture`) treated the last non-empty output line as a question whenever it began with a question word and ended with `?`. It did not exclude warning lines, lines in a Python traceback block, quoted text or code lines. A run whose last line was such a line, followed by a pause longer than the question timeout, was stopped with status 75. The first stop goes to the next rotation entry (INT-004); the second leaves the task BLOCKED. `status` then said only that the provider run ended without a result. A genuine question had the same outcome: the question text was not recorded anywhere, so `status`, the Overview `Needs you` row and the JSON projection could not show it or the ways forward.

The exact Python `SyntaxWarning` text (`<string>:1: SyntaxWarning: "is" with 'int' literal. Did you mean "=="?`) did not match `QUESTION_LINE_RE` before the fix, as INT-004 found, so the tests for that shape pass on the unfixed code as well. The shapes that did match and the tests that fail on the unfixed code are listed below.

Severity: high. A false positive stops a healthy run; a true positive stops the mission with no visible cause.

Fix:

- `common.pending_question(text)` returns the question the output ends on. The last non-empty line does not count when it is a warning (`\w*Warning:`, Node `(node:N)`, `<file>:<line>:` prefix), an exception line, a line inside a `Traceback (most recent call last):` block (through its exception line), inside or opening a code fence, quoted (`>`, `"`, `'`, backtick, `#`, `$`, `|`, `//`) or indented by four spaces or a tab. `ends_with_question` is `pending_question(...) is not None`. `ProgressGuard.note_output` classifies the whole scan buffer instead of the eight-line prompt tail, so a long traceback keeps its header.
- When a run stops with status 75 and cause `question-timeout`, `BridgeMissionHooks.after_iteration` records `task-awaiting-answer` (operation id, task id, provider, question up to 300 characters). `BridgeStore.pending_question` returns it while no later operation has run for the task.
- `project_snapshot` adds `awaiting_answer`. `needs_attention` returns `kind: "question"` with `question`, `actions` (`answer` = `task amend --file`, then `task retry`; `continue` = `task retry`; `reject` = `task cancel`) and a `message` that names all three. `llm-bridge status` and the console Overview `Needs you` row use it.
- `mission-waiting-for-operator` carries the question for a blocked task that asked one. The controller wait therefore always has an event (INT-001) and, for this case, the reason.

Tests (`tests/test_bridge_question_blocking.py`):

- Classifier cases for 16 non-question shapes and 6 genuine questions. Before the fix the quoted, indented, blockquote, code fence and traceback cases fail (`pending_question` did not exist, and `ends_with_question` returned True for the quoted and indented lines).
- Fake headless provider (`run_fresh_exact_stdout`) and PTY provider (`run_pty_capture`) whose last line is a quoted, indented or traceback line, then a pause and `done`: status 75 before the fix, 0 after.
- Fake `codex` through `BridgeMissionHooks` and `ralph_robin.run_scheduler_inline`: a provider that prints a quoted line last and finishes completes the task (before the fix: status 75). A provider that asks a question and stalls leaves the task BLOCKED after the second stop, with `task-awaiting-answer`, `needs_you.kind == "question"`, the actions in `status`, in the console rows and in the projection, and one `mission-waiting-for-operator` event that carries the question while the controller backs off (before the fix: `needs_you.kind == "blocked"`, no event).

Not changed: a question on a task that the operator answers still needs `task amend` plus `task retry` as two steps. There is no `/answer` command.

## Evidence index

Throwaway scripts used for the reproductions (not committed): `/tmp/claude-1000/-home-chris-dev-llm-tools-prd/ff75f2d4-a395-4d74-bf6a-3b81470aa857/scratchpad/repro/blocked.py` (INT-001), `.../reconcile.py` (INT-005), `.../stopcrashed.py` (INT-006), `.../forkexec.py` (INT-009). The INT-004 checks were run inline with `llm_tools.scheduler.ProgressGuard` and `llm_tools.common.QUESTION_LINE_RE`.

## Fix status

| Finding | Severity | Status | Fix and test |
| --- | --- | --- | --- |
| INT-001 | high | Fixed | Blocked and waiting tasks back off with one event, and cancelled tasks count as finished. Tests: `test_int001_blocked_tasks_wait_with_backoff_one_event_and_resume_when_operator_retries`, `test_int001_new_wait_after_the_state_changed_records_a_second_event`, `test_int001_cancelled_tasks_count_as_finished_for_completion`, `test_int001_mission_whose_tasks_are_all_cancelled_stops_instead_of_waiting`, `test_int001_status_says_the_mission_needs_the_operator` (`tests/test_bridge_internals_review.py`). |
| INT-002 | high | Fixed | Controller exception stops the provider process group; a provider that cannot be stopped is recorded as orphaned. Tests: `test_controller_exception_stops_the_provider_process_group`, `test_provider_that_cannot_be_stopped_is_recorded_as_orphaned` (`tests/test_bridge_internals_fixes.py`). |
| INT-003 | high | Fixed | Controller start and `stop --force` retry a failed provider termination, and a reused controller PID is treated as a dead controller. Tests: `test_int003_controller_start_retries_a_failed_provider_termination`, `test_int003_second_force_stop_retries_a_failed_provider_termination`, `test_int003_force_stop_treats_a_reused_controller_pid_as_a_dead_controller` (`tests/test_bridge_internals_review.py`). |
| INT-004 | high | Fixed | A question line followed by more output does not arm the watchdog; tool activity clears a pending question; stderr neither arms nor clears it; a first watchdog stop is handed to the next entry and the second is held. Tests: `test_question_followed_by_more_output_does_not_arm_the_watchdog`, `test_question_at_the_end_of_output_still_arms_the_watchdog`, `test_tool_activity_clears_a_pending_question`, `test_stderr_never_arms_or_clears_the_question_but_can_still_block`, `test_headless_run_that_keeps_working_is_not_aborted_by_an_earlier_question`, `test_headless_run_that_ends_on_an_unanswered_question_is_still_aborted`, `test_pty_run_that_keeps_working_is_not_aborted_by_an_earlier_question`, `test_pty_run_that_ends_on_an_unanswered_question_is_still_aborted`, `test_bridge_hands_a_first_watchdog_stop_to_the_next_entry_and_holds_the_second` (`tests/test_bridge_internals_fixes.py`). |
| INT-005 | medium | Fixed | Crash reconciliation keeps operator CANCELLED and PAUSED tasks and still blocks a RUNNING task. Tests: `test_int005_crash_reconciliation_keeps_operator_cancelled_and_paused_tasks`, `test_int005_crash_reconciliation_still_blocks_a_running_task` (`tests/test_bridge_internals_review.py`). |
| INT-006 | medium | Fixed | `stop` on a RUNNING mission without a live controller ends STOPPED; with a live controller it still requests STOPPING. Tests: `test_int006_stop_on_a_running_mission_without_a_controller_ends_stopped`, `test_int006_stop_with_a_live_controller_still_requests_stopping` (`tests/test_bridge_internals_review.py`). |
| INT-007 | medium | Fixed | A setup failure in `run_controller` after the mission is RUNNING records a failure. Test: `test_int007_setup_failure_after_the_mission_is_running_records_failure` (`tests/test_bridge_internals_review.py`). |
| INT-008 | medium | Fixed | The controller log sink keeps draining when the log cannot be written, and a drain timeout does not replace the exit code. Tests: `test_log_sink_keeps_draining_when_the_log_cannot_be_written`, `test_log_sink_drain_timeout_does_not_replace_the_result` (`tests/test_bridge_internals_fixes.py`). |
| INT-009 | medium | Fixed | A failed `exec` in the `pty.fork` child never returns into caller code. Test: `test_failed_exec_in_the_pty_child_never_returns_into_the_caller` (`tests/test_bridge_internals_fixes.py`). |
| INT-010 | medium | Fixed | A second start does not spawn while the first is launching; the start lock is released once the controller is ready; the lock records controller identity; force stop finds the controller through the lock when the ready file is gone. Tests: `test_a_second_start_does_not_spawn_while_the_first_is_launching`, `test_start_lock_is_released_once_the_controller_is_ready`, `test_controller_records_its_identity_in_the_lock_it_holds`, `test_force_stop_finds_the_controller_through_the_lock_when_the_ready_file_is_gone` (`tests/test_bridge_internals_fixes.py`). |
| INT-011 | medium | Partly fixed | Event thinning, command retention and large-result summaries are fixed. Operations-table pruning is deferred to PRD-010. Tests: `test_pruning_thins_old_wait_and_selection_events_and_keeps_audit_events`, `test_pruning_deletes_only_old_finished_commands`, `test_command_event_carries_a_summary_of_a_large_result` (`tests/test_bridge_internals_fixes.py`). |
| INT-012 | medium | Fixed | The provider exit is recorded even when session capture fails. Test: `test_provider_exit_is_recorded_even_when_session_capture_fails` (`tests/test_bridge_internals_fixes.py`). |
| INT-013 | low | Fixed | Idle controller loops poll without taking the SQLite write lock; SQLite file modes are secured once per connection; worker session reads use a read-only store. Tests: `test_int013_idle_controller_polls_without_taking_the_write_lock`, `test_int013_wait_signature_does_not_promote_tasks`, `test_int013_sqlite_file_modes_are_secured_once_per_connection`, `test_int013_worker_session_reads_through_a_read_only_store` (`tests/test_bridge_internals_review.py`). |
| INT-014 | low | Fixed | `resume` waits out a momentary liveness probe, and a probe that cannot decide reports the lock as held. Tests: `test_int014_resume_waits_out_a_momentary_liveness_probe`, `test_int014_lock_probe_that_cannot_decide_reports_the_lock_as_held` (`tests/test_bridge_internals_review.py`). |
| INT-015 | low | Fixed | Watchdogs ignore wall-clock steps. Tests: `test_progress_guard_ignores_wall_clock_steps`, `test_pty_watchdog_ignores_wall_clock_steps` (`tests/test_bridge_internals_fixes.py`). |
| INT-016 | low | Deferred | The main-loop progress heartbeat and verifier interruption on STOPPING/CANCELLED touch the stop paths. The review does not classify it as a defect by itself. No test. |
| INT-017 | low | Fixed | A failed lock setup does not leave a phantom lock; mission IDs that would name paths outside the runtime directory are rejected. Tests: `test_failed_lock_setup_does_not_leave_a_phantom_lock`, `test_create_mission_rejects_ids_that_would_name_paths_outside_the_runtime_directory` (`tests/test_bridge_internals_fixes.py`). |
| INT-018 | low | Fixed | `cancel` ends the running provider process, names `stop --force` when the provider cannot be ended, and does not signal a reused PID. Tests: `test_int018_cancel_ends_the_running_provider_process`, `test_int018_cancel_names_stop_force_when_the_provider_cannot_be_ended`, `test_int018_cancel_does_not_signal_a_reused_pid` (`tests/test_bridge_internals_review.py`). |
| INT-019 | high | Fixed | Quoted, indented, warning and traceback lines no longer block a headless or PTY task; a genuine unanswered question blocks it with a visible reason and actions. Tests: `test_warning_traceback_and_quoted_lines_are_not_questions`, `test_genuine_questions_are_still_recognised`, `test_progress_guard_uses_the_shared_classifier`, `test_a_long_traceback_cut_to_the_scan_tail_still_does_not_arm_the_guard`, `test_headless_run_ending_on_a_warning_or_quoted_line_is_not_aborted`, `test_pty_run_ending_on_a_warning_or_quoted_line_is_not_aborted`, `test_provider_that_prints_a_warning_last_and_pauses_completes_the_task`, `test_provider_that_asks_a_question_and_stalls_blocks_with_the_question_and_its_actions`, `test_answering_the_question_by_amending_and_retrying_runs_the_task_again` (`tests/test_bridge_question_blocking.py`). |
