# Bridge specification audit

Independent review on 2026-10-26 against `docs/features/llm-bridge/prompt.md`,
baseline `066af5c` and the current working tree. Reviewer: GPT-6.1 Sol / High,
as most recently requested by the user. This is an implementation audit, not a
claim that mocks establish live-provider compatibility. Findings A1–A10 are in
`review-findings.md`; current merge verdict is **NOT READY**.

The tables below preserve the first full-spec checkpoint. The changed-surface
re-review at the end records which initial gaps have since closed; it takes
precedence over their historical OPEN/PARTIAL labels.

**Verified** means an inspected implementation has relevant checked-in tests;
exact commands independently rerun are recorded below. **Partial** means useful
implementation exists but a required behavior/evidence is missing. **Missing**
means no implementation or verification was found. This distinction prevents
an existing test name from being mistaken for an executed acceptance pass.

## Substantial requirements

| Specification | State | Implementation and verification / remaining gap |
| --- | --- | --- |
| §§1,15,16 separate roles, durable handoff, review | Partial | Separate agents and `HANDOVER.md`/review history exist; progress/gates need final refresh. Latest user overrides original reviewer model/effort. |
| §§2–4 simple boundaries, existing tools reused | Verified | `bridge_runtime.BridgeMissionHooks` hooks existing `ralph_robin.main`; scheduler launches, existing usage/route readers decide. `test_bridge_hooks.py`, `test_bridge_end_to_end.py`. No second scheduler/service/network listener. |
| §5 seven useful terminal views | Partial | `bridge_tui.py`, `terminal.py`; real curses PTYs exist. Orders/station inspection and capacity rendering incomplete (A4). |
| §6 keyboard, mouse, focus and discovery | Partial | Prefix/focus transport tested by `test_terminal_attached.py`; navigation/editor tests in `test_bridge_tui_pty.py`. Task inspect/pause/resume and advertised `control` incomplete (A4,A5). |
| §7 native PTY fidelity | Verified for transport; live partial | Private tmux native pane; outer PTY tests cover ANSI/input/paste/mouse/focus/resize/cleanup. Actual providers have narrower evidence in `native-live-evidence.md`; no live native restoration pass. |
| §7.3 viewed/executing/historical stations | Partial | Executing op and last launch distinct. Viewed preference not projected; no selected historical/native station inspection (A4). |
| §8 mission/task durable data and state machine | Partial | SQLite WAL/FULL, original prompts/revisions/dependencies/evidence/history. `test_bridge_store.py`, concurrency and CLI suites. Command atomicity reopened by A1; provenance absent. |
| §8.1 task commands and multiline/file entry | Partial | CLI add/show/amend/priority/reorder/dependency/pause/resume/cancel/retry/verify; editor/file entry tested. TUI task details and amendment preload absent (A4). |
| §8.2 typed safe-boundary steering | Verified | `bridge._apply_operator_effect` + hook boundary requests; `test_bridge_steering.py` covers replace/review/verify/pause/stop/handoff. Amendments invalidate stale success. |
| §8.3 ordering/dependencies/proposals | Partial | Deterministic priorities/order and cycle rejection covered. No source/proposal acceptance semantics (A5). |
| §9 evidence-based completion | Verified | Zero exit -> VERIFYING; operator approval or configured verifier required; artifact digests and revision evidence. `test_bridge_verifier.py`, store/CLI/E2E suites. |
| §9.1 conservative convergence/stall policy | Partial | Bounded provider-failure handoff budget and failed-exit warning. Other required progress indicators/detection absent (A5). |
| §10 capacity/handoff | Partial | Existing Ralph/route selection, bounded durable handoff, independent refresh. `test_bridge_end_to_end.py`, refresh/hooks tests. TUI loses scopes, lacks exhaustion estimate and complete next-route display (A4). |
| §11 pause/resume/detach/attach | Verified for hermetic transport | CLI/hook/outer-PTY tests; active operations finish at a boundary. Separate detached controller owns flock. |
| §11.1 complete managed stop | Partial | Identity checks/force-stop/tmux teardown and failure tests exist. Remaining process-group descendants survive (A2). |
| §11.2 shutdown/reboot reconstruction | Partial | Uncertain operations blocked; tasks/config/evidence survive reopen; real controller restart tests. Effective start overrides not persisted (A3). |
| §11.3 native resume vs reconstruction | Partial/live unverified | Owned Claude/Copilot UUID + exact metadata/CWD, exact argv and new-controller fake E2E. Unsafe Codex recency discovery removed; Codex reconstructs. Actual provider restore remains NOT VERIFIED. |
| §11.4 independent OS suspend/wake | Verified | Existing Ralph paths retained; Bridge waits awake for operator control. `test_bridge_wait_interrupt.py` and legacy suspend suites. |
| §12 persistence/concurrency/private data | Partial | WAL/FULL, transaction events, migrations, flock and worktree exclusion tested. Command rejection atomicity A1 remains. No credentials persisted by Bridge. |
| §13 truthful bounded monitoring | Partial | SQL aggregate/page/latest-kind index queries, separate configured/observed labels, background refresh. Useful TUI detail/history/elapsed/iteration display incomplete (A4,A5). |
| §14 A/B/C test layers | Partial | Extensive hermetic contracts and actual subprocess/outer-PTY tests. Real Codex headless reply passed; other live behavior honestly unverified. New failing reviewer regressions need remediation. |
| §14.2 CI and coverage | Partial | Current checked-in CI installs tmux and preserves global 85%; latest user requires Python 3.11+3.15 and Bridge >=90%. Final frozen-head CI evidence pending root. |
| §§15–16 durable milestones/review | Partial | Ledger/handover/findings exist. Older DONE/CLOSED statements must be reconciled with this full audit. |
| §18 responsiveness/resources | Partial | Bounded projections/log sinks, 10k-queue streaming test, 1000-iteration simulation; no claim of multiweek proof. All-event commands remain unbounded (A5); final metrics pending root. |
| §19 documentation/packaging | Partial | Entry point/version/help/config/README/operations implemented. Actual screenshot and final evidence updates pending; docs must follow repaired UX. |
| §§20–22 completion/report | Partial | Completion cannot be asserted while A1–A4 remain High or substantial spec gaps remain. Root owns final CI/merge/release actions. |

## Definition of done (all 27 items)

| # | State | Evidence or blocker |
| --- | --- | --- |
| 1 installed CLI | Verified | `pyproject.toml`, executable, CLI acceptance and entry-point CI check. |
| 2 durable missions/tasks | Partial | Store/concurrency tests; A1 atomic rejection. |
| 3 runtime task submission/steering | Verified | CLI/E2E/typed-steering tests. |
| 4 responsive/intuitive bridge | Partial | Stress/PTY tests; A4 usability. |
| 5 complete keyboard control | Partial | Missing inspect/task controls, A4/A5. |
| 6 supported mouse | Verified transport | Mouse/focus tests; actual provider response remains unverified. |
| 7 native interfaces | Verified transport | Native pane and attached PTY; live matrix limits apply. |
| 8 reuse orchestration | Verified | Hooks into Ralph/scheduler/usage. |
| 9 accurate capacity display | Partial | Data available in snapshot; TUI drops scope details (A4). |
| 10 evidence-based progress | Verified | VERIFYING gate, verifier/approval/digests. |
| 11 detach/attach | Verified hermetic | Attached outer PTY and persistent controller. |
| 12 complete stop/resume | Partial | A2 descendants, A3 config. |
| 13 logical multiprovider restore | Partial | History persisted; A3 launch overrides and A4 station history. |
| 14 reliable native resume | Partial | Fake exact-ID Claude/Copilot E2E; actual restore NOT VERIFIED. |
| 15 explicit reconstruction | Verified | Handoff labels and session states. |
| 16 safe uncertain recovery | Partial | UNCERTAIN/BLOCKED no replay; A2 cleanup. |
| 17 sleep/wake intact | Verified targeted | Bridge waits awake; no-hook paths retained. Final legacy suite root. |
| 18 legacy compatibility | Partial | Actual outer-PTY attached scheduler resize regression A6; final suite root. |
| 19 unit/contracts pass | Partial | Existing gate pending independent rerun; new review tests RED. |
| 20 PTY tests pass | Pending rerun | Real PTY suites inspected and prior evidence recorded. |
| 21 credential-free CI | Verified design | Fixtures/fake capacity/temp state and CI-installed tmux. |
| 22 real-provider checks honest | Verified evidence limits | Native startup/geometry and one Codex headless reply only; other checks unverified. |
| 23 coverage | Pending final gate | Preserve global85; latest user adds Bridge90 on Python3.11/3.15. |
| 24 no unresolved High/Critical | Missing | A1–A4,A6 open. |
| 25 traceable substantial requirements | Partial | This audit supplies trace; missing behaviors remain explicit. |
| 26 accurate docs | Partial | Update after UX and gate remediation. |
| 27 durable progress/review | Verified with refresh needed | Handover, test matrix, findings and this audit retained. |

## Acceptance scenarios A–S

| Scenario | State | Evidence / remaining limit |
| --- | --- | --- |
| A create | Verified | `test_bridge_cli.py`, CLI acceptance. |
| B keyboard | Partial | Outer PTY/editor/navigation tests; A4/A5 core controls. |
| C mouse | Verified transport | Attached PTY and TUI tests; actual provider behavior NOT VERIFIED. |
| D runtime multiline task | Verified | Detached E2E and editor/file tests. |
| E steering | Verified | Steering suite and amendment E2E. |
| F exhaustion/handoff | Verified fake-process E2E | `test_provider_abort_hands_same_task_to_next_provider`. |
| G all exhausted | Verified fake capacity | Wait interruption/Ralph-hook tests; no real OS sleep performed for this review. |
| H detach/attach | Verified hermetic PTY | `test_terminal_attached.py`. |
| I graceful stop/resume | Partial | Runtime/recovery/native E2E; A2/A3. |
| J reboot | Partial | Reopen/new-controller/absent-process tests; A3 and real reboot not performed. |
| K native restore | Partial | Exact-ID fake controller E2E; actual CLI NOT VERIFIED. |
| L contextual reconstruction | Verified | Hook prompt/uncertain-operation tests. |
| M multiprovider restoration | Partial | Durable operations/frozen routes; A3/A4. |
| N crash recovery | Partial | Real process death/tmux mutator tests; A2 cleanup. |
| O terminal fidelity | Verified deterministic transport | Real outer PTY, input/paste/SGR/focus/resize/cleanup. |
| P long run | Partial | 1000-iteration simulation/stream test; no multiweek claim; A5 unbounded commands. |
| Q existing tools | Partial | Attached scheduler resize A6; full-suite/global85 root. |
| R real providers | Partial, explicitly bounded | Codex headless reply PASS; native resume/input/mouse and other completions NOT VERIFIED. |
| S implementation interruption | Verified records | Handover/progress/review ledger permits deterministic continuation; final ledger refresh needed. |

## Independently executed review checks

- `python -m pytest tests/test_bridge_review_regressions.py -q`: initial three
  tests **3 failed**, confirming cancellation/rollback/descendant defects.
- The added effective-config lifecycle test independently **failed**.
- Broad Bridge/terminal/owned-pane run completed with **four failures, all four
  known-red reviewer regressions**; its existing tests passed. Explicit shell
  glob expansion meant `--ignore` did not omit that reviewer file, so this is
  accurately recorded as a failing gate. The subsequently added attached
  scheduler resize test also independently **failed**.
- No paid-provider completion was invoked by the reviewer.

## Changed-surface re-review after `b11b719`

Independent commands passed **82 tests in 16.72s** for runtime/session/Usage/TUI/
attached PTY and **96 tests in 12.35s** for persistence/concurrency/CLI/verification/
forced-stop/tmux fault paths. Exact commands are in `review-findings.md`.
Independent public-option REPL proposal tests added afterward also passed.

| Requirement / scenario | Updated state | Verification |
| --- | --- | --- |
| Atomic command rejection / durable task state (Done 2; §§8,12) | Verified, A1 closed | Independent failed-task cancellation and general rejection rollback regressions. |
| Complete stop / recovery (Done 12,16; I,N; §§11.1–11.2) | Verified targeted, A2/A7/O7/O8/O10 closed | Actual same-group and independent-session descendants; persisted verifier identity; tmux teardown failure preserves metadata; 96-test gate. |
| Effective configuration restoration (Done 12,13; J,M; §11.2) | Verified, A3 closed | Prior station mode retained until cleanup, explicit mode override and persisted configuration tests. |
| Legacy attached fidelity (Done 7,18; O,Q; §7) | Resize regression closed, final full gate pending | Actual outer PTY receives SIGWINCH after restoring foreground process-group behavior. |
| Task/proposal provenance (Done 3; §§8.2–8.3) | Verified explicit workflow | Durable PENDING proposals cannot be scheduled; same-mission operation provenance; accept/reject commands; terminal missions reject stranded additions/acceptance (A8 closed). |
| Shared CLI/REPL grammar (Done 3,5; B,D,E) | Verified targeted | Public parser/handlers used through a shell-free CLI child; file/options retained; public proposal option grammar repaired (A9 closed). |
| Task inspection / capacity / Usage UI (Done 4,5,9; §§5,6,10,13) | Partial, A4 remains open | Task detail/amendment preload/scopes/estimates and full in-process Usage renderer have targeted tests. Station-view projection and detail scrolling await root/terminal follow-up. |
| Embedded Usage error boundary | Partial, A10 open | Ordinary asynchronous errors/stale cache tested; root is adding SystemExit/configuration failure handling. |
| Native Codex restoration (Done 14; K,R) | Live verified at installed 0.164.0 | Root-owned `scripts/bridge_native_mission_smoke.py` and `native-mission-results.json`: real native reply, full stop, new controller, same owned UUID/RESTORED_NATIVE, new response recalls context, cleanup PASS. Reviewer inspected the oracle and independently observed initial owned-FD provenance. |
| Other live provider capabilities / mouse (Done 6,22; C,R) | Explicitly unverified where not observed | Claude/Copilot exact-ID fake E2E is not a paid live completion. Actual Codex provider mouse remains NOT VERIFIED. |
| Monitoring/convergence/history (§§9.1,13,18) | Partial, A5 follow-up | SQL event paging fixed; root owns selected-station/stall/status evidence. |
| Final coverage/CI/release/docs (Done 18–21,23–27) | Pending frozen head | Latest user adds Bridge90 and Python3.11/3.15 while retaining global85. Root owns final evidence, actual screenshot/README and publication; no premature merge approval. |

The user-added requirements in `../features/llm-bridge/spec-additions.md` are
part of this review: prefix/clickable focus, parser parity, model-scope forecasts,
full embedded Usage, opt-in native probes, separate Bridge90 gate, and actual
screenshot/dogfood evidence. Native probe outcomes must remain capability-specific.
