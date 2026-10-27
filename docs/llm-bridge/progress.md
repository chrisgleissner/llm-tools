# LLM Bridge implementation progress

## Durable contracts

- Architecture, CLI, state transitions and test gates: `architecture.md`, `implementation-plan.md`, `test-matrix.md`.
- Installed CLI capabilities and root-owned live startup/model probes: `provider-capabilities.md`, `provider-startup-results.json`, `provider-model-results.json`.
- Baseline existing suite: `python -m pytest -q` passed before Bridge work.
- User-owned `docs/features/llm-bridge/prompt.md` remains untouched.

## M2: Native terminal backend

- `llm_tools/terminal.py` exposes `TmuxTerminalBackend.start/attach/send_keys/resize/capture/is_alive/stop` and a durable `TerminalHandle` with private socket/config/session/pane IDs.
- Red: `python -m pytest tests/test_terminal_backend.py -q` → collection error `ModuleNotFoundError: llm_tools.terminal` before implementation.
- Green: `python -m pytest tests/test_terminal_backend.py -q` → 2 passed on tmux 3.6.
- Covered: ANSI output, alternate-screen buffers, literal key delivery, resize geometry, private-server isolation, config permissions and cleanup.
- Scheduler integration: `tests/test_scheduler_owned_pane.py` verifies a real command uses an existing private pane, preserves status/output and leaves the server alive. `python -m pytest tests/test_scheduler_owned_pane.py tests/test_additional_paths.py::test_scheduler_system_and_tmux_helpers -q` → 8 passed.
- Root's `tests/test_terminal_attached.py` exercises a real outer PTY, ANSI, keyboard, paste, mouse, resize/SIGWINCH, detach and restoration; terminal-agent follow-up is in progress. Root-owned provider smoke exercised Codex, Claude, Copilot, Kilo and OpenCode startup without model prompts. Three explicit model probes did not return the expected answer by deadline; no automatic retry was made.
- Independent PTY review and prefix/focus hardening are in progress with root's terminal implementor.

## M3: Durable mission/task state

- `llm_tools/bridge_store.py`: SQLite WAL/FULL, schema v3, private state files, transactional missions/tasks/dependencies/amendments/commands/operations/events, idempotent command IDs, lifetime mission flock, Git worktree-root exclusion, prelaunch PREPARED state, process/pane identity, interruption recovery, and evidence bound to task revision.
- Initial red: `python -m pytest tests/test_bridge_store.py -q` → missing-module collection error.
- Initial green: 7 tests passed; reviewer found additional safety gaps.
- Store remediation added regressions for post-exit amendments, shared dependency readiness/completion, nested command transactions, KeyboardInterrupt/commit rollback, and command effect+ack atomicity. Reviewer closed P1-P8 and O1 after the store/atomicity gates passed.
- Latest lifecycle red/green: `python -m pytest tests/test_bridge_store.py -q` first failed 3 operation-exit tests because completion overwrote PAUSED/CANCELLED; `finish_operation` now preserves task or mission pause/cancel intent. `MissionLock.acquire()` is idempotent on the same instance so entering an acquired controller lock does not drop exclusion.
- A further transaction guard rejects PREPARED intents unless the mission is RUNNING; red cases cover PAUSED/STOPPING/STOPPED/CANCELLED. Process identity is PID + boot ID + `/proc` start time and is committed only for a RUNNING operation.
- Covered: ordering/dependencies/cycles, concurrent submissions and controller locks, crash rollback/reopen, uncertain operations, verification gating, revisions and amendment invalidation, command idempotency, commit failure rollback, WAL/SHM permissions, schema migration, and overlapping Git worktrees.
- Root benchmark against persistence checkpoint `219b5f0`: 10k task adds 4.06s, listing 10k 0.097s, 3.887 MB DB, 35,644 KiB peak RSS, integrity ok. Exact output: `performance.md`, `store-benchmark-results.json`.
- Store is frozen again for review. Expanded focused gate: `python -m pytest tests/test_bridge_store.py tests/test_bridge_runtime.py tests/test_bridge_hooks.py tests/test_bridge_cli.py tests/test_bridge_dispatch_atomicity.py tests/test_bridge_concurrency.py -q` → 38 passed before the additional mission-operation guard.

## M4/M5: Active implementation

- Scheduler has additive private `tmux_socket`, exact `tmux_pane` and `native_tui` fields; ordinary `llm-scheduler --tmux` behavior retains the default server path.
- Attached argv uses observed native entrypoints for Codex, Claude, Copilot, Kilo and OpenCode. MiniMax is not presented as a native engineering session; headless `mmx` uses its observed `text chat --message` interface.
- `llm_tools/bridge.py` exposes mission/task/status/events/doctor/control commands and JSON projections; `llm-bridge --file -` reads verbatim stdin.
- Ralph boundary hooks are wired through `llm_tools/bridge_runtime.py`; default Ralph behavior remains unchanged. Operation intent is committed before launch, successful exit enters VERIFYING, and explicit evidence/approval remains required.
- Autonomous runs stay headless even when Bridge uses a tmux control station. `--native` explicitly selects the provider's interactive terminal in the owned pane. Active pause/cancel is durable and reported as DEFERRED until the provider returns; requested PAUSED/CANCELLED task state is preserved on exit.
- Recovery attaches only to a server verified by `TmuxTerminalBackend.open_existing`. The prompt includes mission objective, current task revision, acceptance criteria, bounded recent events/operations, and explicitly labels provider continuity RECONSTRUCTED.
- Configured verifier argv has a bounded timeout, private capped logs, process-group cleanup and revision-bound evidence/artifact digests. Exit code 0 passes unless optional JSON says `passed:false`; JSON may add summary and artifact paths.
- Detached controller readiness uses an atomic private file with process identity and random startup token; launches delete stale readiness and wait for matching child PID/token.
- Root's `tests/test_bridge_cli_acceptance.py` and `tests/test_bridge_end_to_end.py` cover CLI lifecycle and detached submission/amendment/dependency/artifact verification. Root owns real pause/stop/restart E2E additions.

## Still required

1. Complete controller lifecycle tests for forced stop races, resume after controller loss, terminal ownership/reconstruction and controller failure recovery.
2. Review frozen provider/route/model policy (raw TOML snapshot installed under mission-private runtime); inherited environment capacity/auth overrides remain live.
3. Finish native supervised checkpoint UX and record exact native restore support versus reconstruction.
4. Independent runtime/CLI/TUI reviews and remediation, full repository regression and coverage verification.


## Final integration checkpoint, 2026-10-27

Frozen production source at `41cff55` passed 1,254 tests on Python 3.11.14
(199.95s) and 3.15.1 (194.47s). Both passed the exact CI branch-coverage gates:
global 89% (minimum 85%), Bridge/terminal 92% (minimum 90%). Final remediation
below requires a new frozen run; these results are not its final-head evidence.

Independent Sol 6.1 High review found a real JSONL owned-FD header parsing bug
(A21) and a scheduling race in the live probe (A22). The bounded first-record
read and full-stop probe sequence are repaired with actual owned-FD regressions.
The repaired two-prompt Codex probe passed native reply, complete controller
stop/lock release, same owned UUID under a new controller, context recall and
cleanup (`final-native-mission.json`). Equal measured provider viewports also
verify native wheel parity (`final-native-input.json`); current-model mouse
click selection remains unverified on both direct and Bridge paths.

Intermittent actual outer-PTY focus tests and final integrated review remain
open, as do final rebuilt-artifact installation, fresh full runs and CI. No
merge or release has occurred. Original `prompt.md` remains unchanged.


## Frozen publication source verified, 2026-10-27

Final unchanged production/tests passed **1,262 tests** on Python3.11.14 in
196.73s and Python3.15.1 in188.98s. Both exact CI branch-coverage reports pass:
**global89%** (required85%), **Bridge/terminal92%** (required90%). Coverage
includes `llm_tools/bridge*.py,llm_tools/terminal.py`; no gate/exclusion changed.

Sol6.1 High final source review closes A1–A24. Terminal implementor repeats
passed12 consecutive7-test runs per version; reviewer independently passed48
terminal contracts plus12 more attached/dead-server repeats. Final bounded
Codex reply/full-stop/exact-ID resume/context recall/cleanup retake passed.

Final wheel and sdist build succeeded, installed into isolated Python3.11/3.15
environments, and passed CLI help plus private mission create, verbatim prompt,
pending amendment and task inspection checks from outside the source tree.
Original directive and user prompt remain unchanged. See `release-verification.md`
for the source verification record. GitHub CI must pass before publication.
