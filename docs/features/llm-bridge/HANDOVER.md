# LLM Bridge resume handover

## Latest user requirements (publication update)

- Continue until fully merge-ready, then merge PR #26 and publish a new release.
- Full adversarial review against the entire specification; Sol 6.1 reviewer
  MUST use **High** effort (the earlier extra-high fallback was interrupted).
- New Bridge code must pass **90% CI coverage**, retaining the old global85%.
- Validate on current Python3.15 as well as minimum3.11; 3.11 is not a cap.
- Only Codex currently has credits. Test actual Codex and installed Claude CLI
  command propagation without claiming a paid Claude completion.
- Add an actual running-terminal screenshot to README, matching usage docs.
- Dogfood Bridge for further improvements. Any direct-Codex/facade discrepancy
  becomes spec/implementation work. First real mission completed; its verified
  cancellation/rollback patch is now integrated. See `docs/llm-bridge/dogfood.md`.
- Reliable keyboard AND mouse return from any native CLI to Bridge controls;
  terminal agent is implementing explicit outer-multiplexer focus bindings and
  an always-visible clickable control target, including provider-zoom mode.

Current checkpoint: independent dogfood patch integration tests53pass; prior
frozen fullPython3.11 suite941pass withglobal85%. Required newBridge90% has NOT
passed yet. Python3.15.1 installed via current uv runner. Screenshot, CLI proposal
approval wiring, final adversarial remediation and final coverage/CI remain open.
Do not mark PR ready or merge/tag until all new gates actually pass.

## Current authorization (resumed implementation)

The user explicitly revoked the publication freeze: continue until fully done,
make PR #26 fully merge-ready, then **merge and tag a new release**, with diligent
verification first. Do not stop at a draft checkpoint. Pushes/PR updates/merge
and release tagging are authorized. Existing release workflow matches plain
numeric tags (e.g. `1.1.0`, not `v1.1.0`); latest release at resume is `1.0.0`.
Do not merge/tag until all substantial requirements and independent high/critical
findings are resolved, full regression/85% coverage and CI pass on final head.

Resumed gates: short private sockets + native graceful stop/crash recovery +
real two-provider abort/handoff independently passed 3 tests (3.48 s). Native
exact-ID/session integration and actual curses PTYs passed a terminal-owned
100-test focused suite; independent O9 review is active. Monitor has indexed
latest-event queries and truthful mode/model labels under re-review. Typed
steering has seven focused tests. Main owns runtime/store again.

Root real Codex headless reply through Bridge passed (7.13 s). Claude/Copilot
remain NOT VERIFIED; Claude exposed force-stop leaving an idle controller alive.
Root safely killed only matching recorded probe PID/start-token group. Main's
new real controller-group/lock tests pass and enforce release before STOPPED.
Root actual command-pane load test passes with 10,000 tasks and sustained fake
output; detailed metrics are being recorded. A diagnostic moving-source coverage
run was 78%; frozen final coverage must still pass unchanged 85%.

Resumed milestones: short private sockets, native graceful stop/crash recovery,
and real two-provider handoff independently passed 3 tests in 3.48 s. Native
session persistence/exact-ID resume across controller restart, real curses PTYs,
monitor paging and managed wait interrupts passed a terminal-owned 100-test
focused gate; independent native review is underway. Main owns runtime/store
again after terminal integration. Typed steering has seven focused tests.

Real Codex through Bridge returned the expected harmless reply; cleanup passed
(7.13 s). Claude/Copilot remain NOT VERIFIED. Claude exposed a force-stop bug:
mission STOPPED but idle controller still owned flock. Root verified the recorded
PID/start token, terminated only that probe's group, and confirmed lock release.
Main is adding a regression/fix. Diagnostic coverage during concurrent source
edits was 78%; a frozen full rerun remains mandatory, with no gate reduction.

Updated 2026-10-25. Implementation is NOT complete. Preserve adjacent user-owned `prompt.md` unchanged. Read `docs/llm-bridge/progress.md`, `architecture.md` and `review-findings.md` before resuming. This handover is updated at each major milestone at the user's request.

## Objective and decisions

Complete production `llm-bridge` in `/home/chris/dev/llm-tools` while preserving
existing tools. Continue current work; do not repeat M0 investigation.
Architecture: stdlib SQLite WAL/FULL, private tmux PTYs, durable Bridge
tasks/commands/evidence; Ralph retains iteration/selection/suspend, scheduler
retains launching/retries. Autonomous mode uses headless providers and an actual
log feed. `--native` explicitly selects supervised native UI. No VT emulator,
web server or duplicate scheduler. Continuity is currently `RECONSTRUCTED`,
never exact native restoration without verified identity.

Models actually used in separate contexts: planner `gpt-6.1-sol`/`medium`,
implementor `gpt-6-luna`/`high`, reviewer `gpt-6-luna`/`xhigh`; another Luna/high
implementation context handles terminal/packaging. No substitutions needed.

## Last verified checkpoints

- `219b5f0`: initial durable store (subsequent reviewed fixes remain in working tree).
- `a8e6bf6`: architecture, provider evidence, store benchmark, cross-process tests.
- `53eea0a`: command atomicity, CLI acceptance and detached workflow tests.
- `f60adef`: executable/entrypoint, README/operations/config/AGENTS/CI docs.
- Independent persistence gate closed at 28 focused tests, then reopened for
  verifier/process-identity changes; review the newest diff before accepting it.
- Terminal/TUI/owned-pane suites independently passed 26 tests. Attached PTY
  passed ten consecutive runs after resize/input synchronization. Covers native
  ANSI, input, paste, SGR mouse/focus, SIGWINCH, Ctrl-B, Ctrl-] and cleanup.
- Root's public CLI acceptance: seven passed. Detached real-controller test
  passed in 0.74 s: add/amend during execution, three fake provider increments,
  dependency unlock, artifact verification, mission completion and lock release.
- Latest broader targeted suite reported 84 passed by terminal implementor;
  full final regression/coverage has NOT yet run.
- Installed command verified in a temporary venv; system editable install was
  blocked by PEP 668. Direct help and harmless mission create/status passed.

## Active work and blockers

Main implementor owns `bridge.py`, `bridge_runtime.py`, `bridge_store.py`,
`common.py`, Ralph/scheduler hooks and their tests. Root owns this handover,
provider/benchmark scripts and CLI/concurrency/dispatch/end-to-end acceptance
tests. Reviewer owns `docs/llm-bridge/review-findings.md`. Terminal and packaging
work is handed back. Agents may not exist in a new session; files/tests prevail.

Unfinished execution/recovery gate (check latest review dispositions):

1. O3: atomically reject launch intent unless mission is RUNNING; check added,
   independent re-review still required. Preserve no-hook legacy behavior.
2. O4: stop/pause responsiveness inside provider-capacity/readiness/suspend waits.
3. O5: active task cancel must report deferred work truthfully and retain state.
4. O6: ready file needs fresh nonce plus exact spawned-controller identity;
   stale status alone must never count as startup success (fix in progress).
5. Graceful stop must terminate owned tmux/UI and restore attached terminal.
6. Mission pause must not strand successful active work as PAUSED forever;
   prefer task VERIFYING while mission remains PAUSED. Root adding a real
   pause/stop/full-restart/no-replay test in `test_bridge_end_to_end.py`.
7. Verify headless force-stop PID+boot/start token/PGID ownership, uncertainty
   before signals, bounded cleanup, refusal when ownership is unproven.
8. Verifier output and descendant processes need bounded storage/memory/cleanup;
   evidence must bind the executed revision, not a concurrent amendment.
9. Complete sessions/stations/capacity monitoring and conservative stall controls;
   audit requirements against actual implementation, not just available views.

## Verification and exact resume procedure

```bash
cd /home/chris/dev/llm-tools
/home/chris/.agents/skills/agent-bridge/scripts/bridge inbox codex
git status --short
git log -6 --oneline
cat docs/features/llm-bridge/HANDOVER.md
cat docs/llm-bridge/progress.md docs/llm-bridge/review-findings.md
python -m pytest tests/test_bridge_store.py tests/test_bridge_concurrency.py tests/test_bridge_dispatch_atomicity.py -q
python -m pytest tests/test_terminal_backend.py tests/test_terminal_attached.py tests/test_bridge_tui.py tests/test_scheduler_owned_pane.py -q
python -m pytest tests/test_bridge_end_to_end.py -o addopts='' -q --basetemp=/tmp/llm-bridge-resume-e2e
```

Inspect uncommitted code first; several production modules/tests have not yet
been checkpointed. Concurrent agent pytest runs can delete one another's default
`/tmp/pytest-of-*` histories; use a distinct `--basetemp` for debugging and wait
for controller flock release in test cleanup. Never signal a PID without its
recorded start identity. Tests must use hermetic fixtures and fake provider
commands, never live credentials. Re-run the smallest affected tests, finish
open review findings, then final `coverage run -m pytest`, `coverage combine`,
`coverage report --fail-under=85`, syntax/package/help checks and full review.
Do not weaken the 85% gate or count skips as passes. Update this handover and
progress at each next major milestone, then commit coherent reviewed changes.

## Real providers and performance

Five native CLIs produced startup output through the production tmux backend;
cleanup and pane geometry passed. Three initial harmless model prompts returned
no expected answer by deadline: model execution/native resume/provider mouse
remain NOT VERIFIED. No automatic paid retry was made. Exact versions and raw
non-sensitive result JSON are in `docs/llm-bridge/provider-capabilities.md`.
MiniMax mmx 1.0.16 is text chat, not a native autonomous engineering session.

Store-only measured benchmark: 10k durable tasks 4.06 s, list 97 ms, command
commit 0.37 ms, integrity ok; see `performance.md`. This does not verify UI
latency under 1 MiB/s output or multi-day reliability. These remain open.

## Publication checkpoint requested by user

User requested immediate push of all changes and a PR because credits are low.
Work is frozen on `feat/llm-bridge`; publish as DRAFT, not merge-ready.
Published draft PR: https://github.com/chrisgleissner/llm-tools/pull/26
The branch is pushed to origin. Resume with `git switch feat/llm-bridge`
and `git pull --ff-only`, or `gh pr checkout 26` in a fresh checkout.
All relevant implementation, tests, ledgers and unchanged original prompt are
included. Packaging checkpoint is `f60adef`; this handover started at `de32b6d`.

Latest root acceptance command: `python -m pytest tests/test_bridge_concurrency.py
tests/test_bridge_cli_acceptance.py tests/test_bridge_dispatch_atomicity.py
tests/test_bridge_end_to_end.py -o addopts='' -q
--basetemp=/tmp/llm-bridge-root-acceptance-01` -> **15 passed in 3.14 s**.
This includes pause during work, provider exit to VERIFYING, graceful headless
stop, complete controller shutdown, resume, manual verification, no replay.
Compileall and `git diff --check` passed at publication freeze.

Main implementor reports full repository pytest passed before the newest two
native recovery tests. Those two currently FAIL during tmux startup; observed
socket path length is 128 characters, likely exceeding Unix socket limits.
FIRST NEXT ACTION: shorten private socket paths under validated XDG_RUNTIME_DIR
(keep durable metadata under XDG_STATE_HOME), expose tmux stderr, then rerun
`tests/test_bridge_recovery_tmux.py`. Its graceful-stop expectation was corrected
to require terminating UI/station, not retaining them. These tests remain red.

Open review findings: O3 task-cancel/pause interleave before launch; O4 wait-loop
stop/pause responsiveness; O5 truthful active cancellation; O7 graceful tmux
cleanup; O8 surviving native provider reconciliation; O9 native IDs/restoration.
Native resume remains unimplemented/unverified. Selection/handoff/wait events
now persist, but station/capacity/controller/health status projections and stall
controls remain unfinished. Final coverage/CI/performance gates are NOT passed.
Resolve blockers, rerun full suite and 85% coverage before making PR ready.
