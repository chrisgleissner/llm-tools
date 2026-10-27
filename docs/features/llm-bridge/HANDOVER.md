# LLM Bridge resume handover

## CURRENT — 2026-10-27 publication checkpoint

Resume the existing implementation in `/home/chris/dev/llm-tools`, branch
`feat/llm-bridge`, draft PR https://github.com/chrisgleissner/llm-tools/pull/26.
The user explicitly requested this handover and a push of ALL current changes.
This checkpoint extends parent `37cbc2b35ed8a0d2af5e4f8ed352c700f3cf5951`;
use `git log -1` for its resulting commit. Preserve the original `prompt.md`.
Historical records below are superseded by this section.

OBJECTIVE
Finish the full specification, adversarial review and final verification. User
authorizes push/PR updates and eventual merge/release, but only after actual
merge-readiness. Keep PR draft until global85% and Bridge90% coverage, Python
3.11/3.15 CI, independent review and documentation/build gates pass. Release
workflow uses plain numeric tags (candidate `1.1.0`), not `v1.1.0`.

RELEVANT FILES
Production: `llm_tools/bridge*.py`, `terminal.py`, `scheduler.py`, `usage.py`.
Contracts: `tests/test_bridge*`, `test_terminal*`, scheduler timeout regressions.
Records: `docs/llm-bridge/{progress,review-findings,spec-audit,test-matrix}.md`,
`native-input-evidence.md`, native mission/reply/input result JSON files.
README includes a real running Bridge/native Codex screenshot, not a mockup.

DECISIONS
Standard-library Python, SQLite WAL/FULL, private tmux native PTYs, flock single
controller. Ralph owns autonomous iterations/routes/capacity/suspend; scheduler
owns launching; usage owns quota/history/forecast math. Native supervised mode
is explicit; autonomous/headless mode is distinct. Actual Python minimum3.11
and current3.15 supported; never describe3.11 as the maximum. Planner Sol6.1
Medium, implementors Luna High, final independent reviewer Sol6.1 High (latest
user requirement). Existing agent contexts may no longer exist after resume.

INVARIANTS
Unknown interrupted outcomes BLOCKED, never silently replayed or completed.
Never infer provider ownership from newest unrelated session. Exact owned ID,
cwd and metadata checks gate native restoration; reconstruction labelled
explicitly. CLI and REPL share parser/handlers. Provider keys/paste remain
native; Ctrl-] B/P and persistent mouse targets select Bridge/provider focus.
No credentials/raw sensitive transcripts in repository. No exclusions or gate
reductions to manufacture90% coverage. Skipped/unavailable live checks are NOT
VERIFIED. No paid Claude/Copilot prompts while only Codex has credits.

IMPLEMENTED CHANGES
- Durable proposals/approval, steering, verifier/process-tree shutdown, explicit
  restoration states; native mode inheritance on resume and unbounded owned
  pane waits (legacy timeout semantics preserved).
- Exact writable-client attach/detach across private servers, typed terminal
  actions after curses teardown, readonly/multiple-writer safety, explicit
  unzoom and keyboard/mouse focus bindings. Real outer-PTY A-to-B-to-A tests.
- Bounded scrollable REPL response overlay for help/JSON, valid palette commands,
  full verbatim Ctrl-R/Ctrl-E editing (20k+CRLF/tabs preserved); shared in-process
  task dispatch and safe rejection of curses stdin file reads.
- In-process full Usage view without even an ephemeral llm-usage process;
  history sampling and bounded forecast cache; active/alternative 5h/weekly
  estimates, anchored/indexed log paging and station history.
- FIFO command/handoff chronology uses insertion order plus indexes rather than
  timestamps/UUID ordering. Conservative progress/convergence evidence.
- Actual Claude native user/assistant transcript metadata and canonical project
  path encoding; malformed metadata/sidechain/absent cwd fail safely. Exact
  Codex-ID restore scans existing bounded directory budget rather than32 only.

TEST EVIDENCE
A broad Python3.11 run observed1221PASS in189.37s, but source changed during it:
this is diagnostic, NOT final frozen-head coverage evidence. Fresh scoped
runtime92%, CLI90%, store92%, TUI90%, terminal90% were observed by implementors;
combined final Bridge90% is still NOT VERIFIED. Sessions diagnostic79% preceded
additional26 passing boundary tests; rerun the complete coverage gate.
Actual full Bridge Codex0.164.0 native reply and exact-ID resume in a fresh
controller with recalled context and cleanup passed using two model calls.
Claude2.1.310 /help and Codex /model propagated in Bridge/direct comparison
without model calls. Known-marker scrolling observed through Bridge; mouse
model-picker click has no visible effect on either path and is NOT VERIFIED.
No paid Claude/Copilot completion/native restoration claim. Check latest
checkpoint live-probe addendum below for immediate pre-commit retakes.
Long-run1000tasks/1009attempts passed35.34s (retained2.19MB, peak2.65MB,
DB5.04MB); instrumented10k-task streaming input gate passed with max409ms,
CI threshold750ms. These are measured tests, not multiweek reliability proof.

KNOWN RISKS
Final aggregate coverage and frozen full3.11/3.15 CI-equivalent runs remain
required. Reviewer closed A13-A20 in the findings ledger; final integrated
review including latest chronology/response changes still required. README
native-resume wording and operations docs need reconciliation with verified
behavior. Rebuild/reinstall final artifacts. First real dogfood mission through
Bridge completed and its fix was integrated (`dogfood.md`); a second inherited
profile/commit dogfood was only planned, never started. No merge/tag yet.

NEXT ACTION
1. Read this CURRENT section, git status/log, progress/review-findings/spec-audit;
   check `~/.agents/skills/agent-bridge/scripts/bridge inbox codex`. Preserve all
   existing work; do not restart repository investigation.
2. Freeze source, run full pytest+coverage on3.11 and3.15, report global85 and
   scoped Bridge90 exactly as CI defines; fix meaningful gaps, never exclusions.
3. Complete final Sol6.1/High independent review and targeted remediation;
   reconcile docs, rebuild/install wheel+sdist and inspect CLI help.
4. Rerun bounded ad-hoc native reply/resume/input probes before major commits;
   update evidence truthfully. Push, refresh PR, inspect all CI/review threads.
5. Only once every required gate passes, mark ready, merge, tag/release1.1.0
   if still the next available version. Verify published artifacts.

Resume tools: `/tmp/llm-bridge-release-venv/bin/python` (3.11.14),
`/tmp/llm-bridge-release-315-venv/bin/python` (3.15.1). Recreate if unavailable.
Use unique COVERAGE_FILE directories and --basetemp; keep pytest log names
separate from coverage prefix because parallel coverage combine scans suffixes.
Example full run: `COVERAGE_FILE=/tmp/bridge-final-311/.coverage
/tmp/llm-bridge-release-venv/bin/python -m coverage run -m pytest -o addopts='' -q
--basetemp=/tmp/bridge-final-311/tests` (create directory first); combine/report
using the same COVERAGE_FILE and exact CI scoped90 include expression.
Live scripts: `python scripts/bridge_native_mission_smoke.py
--model-call-budget 2 --seconds 35 --output /tmp/native-mission.json` and
`python scripts/bridge_native_input_probe.py --help` (requires explicitly owned
Codex UUID/cwd, never an unrelated user session).

### Immediate pre-commit verification (2026-10-27)

54 focused current-source regressions PASS in1.55s: session boundaries,
chronology, start mode, owned-pane/legacy timeouts, interactive attach, native
metadata, input-probe oracle and embedded Usage. `git diff --check` passed.
The ad-hoc two-call-budget native mission retake returned native_reply PASS,
native_resume NOT VERIFIED, cleanup PASS (script exit1). This is a new unresolved
verification gate despite the earlier successful resume run. Investigate the
bounded probe/resume path before release; do not replace this with the earlier
PASS. Exact structured result: `docs/llm-bridge/checkpoint-native-mission.json`.
Zero-prompt native input retake passed Codex /model and Claude /help in direct
and Bridge paths; cleanup PASS. Bridge wheel moved the known marker, direct
wheel did not; mouse click changed Bridge screen but not direct screen, with
picker still visible on both. Mouse parity/selection is NOT VERIFIED and needs
investigation. Exact result: `docs/llm-bridge/checkpoint-native-input.json`.

## Historical records (superseded where CURRENT differs)

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

## 2026-10-26 post-reset checkpoint (supersedes older open-state summaries)

Native full-controller Codex reply + exact owned UUID resume + clean controller
shutdown **PASS**: `scripts/bridge_native_mission_smoke.py`, checked-in
`docs/llm-bridge/native-mission-results.json`. Two prompt budget, Codex0.164.0.
Provider mouse remains NOT VERIFIED; actual Codex model-picker SGR click did
not trigger an effort picker. CI oracle tests reject echoes/substrings/history
as successful model replies. No paid Claude/Copilot attempts were repeated.

Owned Codex writable-FD discovery is now wired to native argv/runtime. It uses
capability-gated --no-daemon, exact managed PID/start tokens, periodic scheduler
observation, and safe ambiguity fallback. Fake native controller/restart suite
for Claude/Copilot/Codex + session/owned-pane contracts:23passed17.16s.

Private tmux status focus wrappers used unsupported click styles; root removed
them. Actual outer-PTY keyboard/paste/mouse/zoom/resizing test passes1.97s.
TUI+PTY+backend31passed1.59s; fresh10kstreamingREPLstresspassed7.82s. These are
focused evidence, not final frozen-head gates.

New user requirements are in spec-additions.md: full in-process llm-usage view,
active/alternate5hweekly runout estimates, sharedCLI/REPLgrammar, CtrlEeditor
retaining taskoptions. Root added usage.dashboard_snapshot private-stream API,
bridge_usage nonblocking single-inflight cache, F8Usage view/dynamiczoomedfullUI,
control/usage interactive expand, and reused one selection projection/forecast.
Usage/API/refresh/TUI focused34passed. Runtime/store/CLI agents own their files;
terminal agent ownsTUI/terminal/sessions; root currently ownsmonitor/usage/scripts,
docs/screenshot. Independent Sol6.1High reviewer resumed after capacityreset.

Last diagnostic fullPython311:985passed1failed while UI was changing; full315:
989passed5failed while REPL/UI changed. The statusfocus issue is fixed and
focusedCLI315retakes passed except oldresponse-shape test nowupdated. CI on
pushedb11b719 passed global85 but failed requiredBridge90 (80%). Do NOT weaken
coveragegate, markready, merge/tagorclaimfinalpass. Finish purposefulcoverage,
monitor/stall/history/UI gaps, screenshot, finalindependentreview and full311/
315coverage before publication. New native script must retain recovery metadata
if cleanup fails (currently TemporaryDirectory must be hardened).
