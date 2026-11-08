# Handover prompt: finish LLM Bridge 1.5.0

Written 2026-11-08 09:55 by the overseer session that ran the 1.5.0 missions. The prompt is everything below the second horizontal line. It can be pasted into a plain Claude Code session started in `~/dev/llm-tools`, or run as an LLM Bridge mission that sleeps the computer while the Claude 5-hour window is exhausted (preferred, see below).

---

## Running the work as an LLM Bridge mission with suspend on rate limit

The work runs as one Bridge mission with one Bridge task per work item. Bridge then holds the progress state, orders the tasks, records evidence, and sleeps the computer while the Claude 5-hour window is exhausted. The text after the second horizontal line is the shared brief that every task reads first.

### What Bridge provides, and what is not yet proven

- A native Claude Code mission detects the usage-limit notice in the provider's terminal (`Usage limit reached · limit resets …`). It records `provider-usage-limit`, stops the provider and marks it rate-limited until the reset time. With no other ready provider in the rotation, the mission waits for capacity (`docs/llm-bridge/operations.md`, native quota section).
- With `--suspend-when-rate-limited on`, that wait suspends the machine through the shared Ralph suspend path. The path arms and verifies an RTC wake for the reset time and refuses to sleep when no wake can be armed. After resume the controller continues the task (`BridgeMissionHooks.suspend_allowed` in `llm_tools/bridge_runtime.py`, `suspend_block_reason` in `llm_tools/bridge_power.py`).
- Suspend is refused while any Bridge operation in any mission is active, while a managed process runs, or while an OS sleep inhibitor is held. Other running missions therefore keep the machine awake, which is one reason for the cleanup below.
- No test covers the whole chain from the usage-limit notice through sleep and resume (`tests/test_bridge_power.py` covers the suspend gate only). Task 1 proves it before suspend is enabled.
- Use the rotation `claude` only. With `claude,codex` the mission switches to Codex instead of sleeping, and Codex had 13% of its weekly limit left at handover.

### Weekly limits come first

At handover the weekly windows were almost spent: Claude 4% left, Codex 7% left. Suspend on rate limit then waits for the weekly reset, which can be days away, not for the 5-hour reset. Check `llm-usage` before starting. Start the mission only once the Claude weekly window has reset or has enough left for at least task 1 and work item 1.

### Known defect found while stopping

`llm-bridge finish` typed `/quit` into the Codex pane of `abffc02b` but the command was not submitted, so the provider kept running and the stop needed `--force`. File it as a DOG row (finish must submit the exit command and confirm the provider exited) and fix it in work item 5.

### Step 1: clean up the missions of this work

The missions below belong to the distributed Bridge and 1.5.0 work. Missions not listed (C64 reviews, release-notes and teams missions) are unrelated and stay untouched.

| Mission | State at handover | Action |
|---|---|---|
| `48ead01a` Release readiness | STOPPED (force) 10:06 | push, cancel |
| `94912428` Distributed follow-up (Codex) | STOPPED (force) 10:06 | push, cancel |
| `abffc02b` Pi Zero 2 W verification | STOPPED (force) 10:05, work done | cancel |
| `13ab4055` Pi remote operation verification | CREATED | cancel |
| `62fd1b9c` x | CREATED | cancel |
| `a325bc58` Distributed LLM-Bridge | STOPPED | cancel |
| `50bcb24b` Productionize LLM Bridge | CREATED | check it is superseded, then cancel |
| `dd3800e2`, `956536d5` Demo | CANCELLED | none |

The overseer force-stopped the three running missions at 10:05 to 10:06 on 2026-11-08, killed the release mission's leftover Playwright loop (`~/llm-tools-infra/redesign-final/chain.sh` and its container) and three leaked chaos-test workers. Their in-flight work was interrupted: the #57 redesign verification run, the 50 repeat runs of the mobile Firefox worker test, the tmux start and soak flake agent, and Codex's #64 edits. In each worktree that mission used, run `git status` and `git log @{u}..` and push any commits that are not yet on the remote. Do not discard uncommitted changes: commit them to their branch as work in progress, or record them in the matching task. Worktrees with unpushed work at handover include `~/llm-tools-infra/wt/redesign` (`feat/web-redesign`), `~/llm-tools-infra/wt/prd038` (`feat/prd038-mission-delete`) and `~/dev/llm-tools-secapi` (`feat/security-api-completion`). Then run `llm-bridge cancel --mission ID` for every mission in the table.

`llm-bridge` on main has no mission delete yet. PRD-038 adds `llm-bridge mission delete`, and the last task deletes all of these missions once it is merged. Afterwards `llm-bridge mission list` shows only the new mission and the unrelated ones. Also check `systemd-inhibit --list` for `sleep` blockers (none were held at handover).

### Step 2: create the mission and its tasks

```bash
cd ~/dev/llm-tools
git -C ~/dev/llm-tools fetch -q origin
{ git -C ~/dev/llm-tools show origin/main:docs/distributed/HANDOVER.md 2>/dev/null \
  || git -C ~/dev/llm-tools show origin/feat/web-redesign:docs/distributed/HANDOVER.md; } > ~/llm-tools-infra/watch/HANDOVER.md
awk 'n >= 2; /^---$/ { n++ }' ~/llm-tools-infra/watch/HANDOVER.md > ~/llm-tools-infra/watch/handover-brief.md
B=~/llm-tools-infra/watch/handover-brief.md
M=$(./llm-bridge mission create --json --name "Finish 1.5.0" --native --providers claude \
      --auto-continue --cwd ~/dev/llm-tools \
      --prompt "Task 1. Read $B first and follow its rules. Prove the suspend chain as described in the Task 1 paragraph of ~/llm-tools-infra/watch/HANDOVER.md, add the missing test, then run: llm-bridge mission set --suspend-when-rate-limited on" \
      --acceptance "provider-usage-limit, suspend-decision, suspend-intent, suspend-outcome and suspend-work-resumed observed in a simulated run" \
      --acceptance "a test covers the chain and fails when the chain is broken" | jq -r .mission_id)
add() { ./llm-bridge task add --mission "$M" --prompt "Work item $1 of $B. Read $B first and follow its rules." --title "$2" --depends-on "$3" --acceptance "$4"; }
add 1 "Merge #57 redesign"            1   "#57 merged, UXR rows [x] with proof"
add 2 "Merge #71 flake fixes"         1   "#71 merged"
add 3 "Merge #64 security and API"    1   "#64 merged, API SEC2 PK rows [x] or waived"
add 4 "PRD-038 mission delete"        1   "mission delete merged with CLI, web and REST parity"
add 5 "Remaining PRD PAR UX DOG rows" 5   "those rows [x] on main"
add 6 "Settle PI rows"                1   "PI rows [x] or waived on main"
add 7 "Final review REV-002..008"     2,3,4,6,7 "REV rows [x] on main"
add 8 "Close out and release 1.5.0"   8   "all rows [x], v1.5.0 tagged and released, stray missions deleted"
./llm-bridge start --mission "$M"
```

Check the task numbers with `llm-bridge task list --mission "$M"` and the field name of the mission ID in the `--json` output before relying on them; adjust `--depends-on` if the numbering differs. Work item 5 depends on work item 4 so that new rows start from a main that already has `mission delete`.

The Task 1 simulated proof: run a short native mission against a fake capacity file (`LLM_SCHEDULER_USAGE_JSON_FILE`, as `scripts/bridge_demo.py --storyboard limits` does) with `LLM_SCHEDULER_NO_ACTUAL_SUSPEND=1`, and confirm the events listed in its acceptance criteria and that the task continues. Optionally prove one real cycle with `./llm-sleep-soak --cycles 1 --period 3m`. This host has had flaky NVIDIA/xHCI resumes; if a wake lands far from its target, Ralph latches to awake-only waits for the rest of the run, so the mission still finishes, only without sleeping.

### Step 3: watch

Use `llm-bridge status --mission "$M"`, `llm-bridge events --mission "$M"` or the web console. After each wake, check that the events show `suspend-outcome` with `actual_sleep` true and that the task resumed. The Pi coordinator and the web console stay up while the desktop sleeps; the desktop worker reconnects after resume.

---

You lead the work that finishes LLM Bridge release 1.5.0, delegating it to subagents. Keep token use minimal. Release 1.5.0 is done when every row in `docs/distributed/SPEC_STATUS.md` is `[x]` (or waived by the operator, with the waiver recorded in the row), main CI and the Pages deployment are green, the tag `v1.5.0` exists and the GitHub release is published.

## Summary of the state at handover

- Main is at or after `a33d776` (PR #69, DOG-024 non-blocking backups, merged 2026-11-08 08:05). PRs #35, #50, #51 to #56, #58 to #63, #65 to #70 are merged.
- Open PRs:
  - #57 `feat/web-redesign` (PRD-037, UXR-001 to UXR-046, plus the DOG row for live lists that patch rows in place). Ready for review, 3 Playwright jobs failing at `2ae30c2`. Newer commits exist in the worktree `~/llm-tools-infra/wt/redesign`. Remaining steps: rerun the redraw tests, run the worker spec 50 times on mobile Firefox without the click retry added by PR #70, remove that retry, get CI green, check the register, the first-load size limits and DOG-026, merge.
  - #64 `feat/security-api-completion` (API, SEC2 and PK rows), draft. All 21 checks passed at `d7a460b`; later pushes not yet checked. The Codex mission that owned it is stopped; its unpushed work is in `~/dev/llm-tools-secapi`.
  - #71 `fix/flake-tmux-start-soak`: fixes two intermittent tests (tmux outer socket reuse, short soak floors).
- Unmerged branch with work in progress: `feat/prd038-mission-delete` (worktree `~/llm-tools-infra/wt/prd038`), PRD-038 `llm-bridge mission delete` with CLI, web and REST parity.
- Register on main at handover (done / in progress / open / blocked):
  PRD 3/15/20, UXR 0/0/46 (all on #57), API 0/16/0, SEC2 4/15/1, PK 4/4/4, UX 0/11/0, PAR 2/3/9, REV 1/0/7, DOG 20/2/1, PI 55/13/1/5.
- Not yet in any PR: the remaining PRD and PAR rows, the UX rows, DOG-021 (125 KB `mission.status` replies every ~10 s and replay of queued commands after a reconnect), worker memory growth, and REV-002 to REV-008.

## Operator decisions that stand (do not reopen)

- No phone passkey tests: PK-012 and PI-VERIFY-013 are waived.
- No 24-hour re-measurement on the Pi: DOG-023 is closed on local measurements.
- Merge each PR as soon as its rows are `[x]` and its checks are green. No new scope.
- Delete the stray missions `dd3800e2`, `956536d5` and `62fd1b9c` once PRD-038 `mission delete` is merged, using that command.
- The full list is in `~/llm-tools-infra/watch/operator-decisions-2311.md`. The PRD-037 design decisions are in `~/llm-tools-infra/watch/prd037-overseer-review.md`.

## How to work

Use the Agent tool for subagents. Do not use agent teams: each teammate is a separate full session and costs far more tokens.

- Run one subagent per work item below, in this order. Run two at once only when they touch different worktrees and neither waits on the other.
- Use `model: "sonnet"` for implementation. Use `model: "haiku"` for mechanical work: counting register rows, waiting on CI, collecting a failing log. Do not use Opus subagents, except for the final review if Sonnet's review is not convincing.
- Give each subagent a short brief: the goal, the worktree, the rows it must tick, the rules below, and "report in under 15 lines". Do not paste this whole file.
- The lead coordinates, reviews diffs and merges. It does not implement and does not read large files or logs itself.
- Wait for CI with one background `gh run watch <id> --interval 300` per run instead of repeated checks.
- The earlier missions 48ead01a, 94912428 and abffc02b are stopped and, after setup step 1, cancelled. Do not restart them; their unfinished work is in the work items below.

## Work items

Each work item is one Bridge task in the mission (task numbers are the item number plus one; task 1 proves the suspend chain). Work only on the item your task names.

1. **#57 redesign:** finish and merge it as listed above, in `~/llm-tools-infra/wt/redesign` only.
2. **#71:** merge it when green.
3. **#64 security and API:** finish the API, SEC2 and PK rows (API-013 redaction-list parity) in `~/dev/llm-tools-secapi`, starting from the work Codex left there, then merge.
4. **PRD-038:** finish `feat/prd038-mission-delete` and merge it.
5. **Remaining rows:** the open PRD, PAR and UX rows, DOG-021 and the worker memory growth. One PR per coherent group, each started from current main.
6. **PI rows:** settle the open PI rows from existing evidence or the recorded waivers.
7. **Final review:** after everything else is merged, run REV-002 to REV-008 against main.

Work item 8, close out:

1. Update the controllers' main checkout to the final main.
2. Remove the worker hotfix drop-in `~/.config/systemd/user/llm-bridge-worker.service.d/hotfix.conf` and restarts the worker.
3. Delete every mission cancelled in step 1 of the setup, including `dd3800e2`, `956536d5` and `62fd1b9c`, with `llm-bridge mission delete`.
4. Tag `v1.5.0` and publish the GitHub release.

## Running across usage-limit pauses

This brief runs inside an LLM Bridge mission that stops the session when the Claude 5-hour window is exhausted, sleeps the computer and continues after the reset. Do not work around a usage limit. Bridge holds the progress state: record evidence on your task with `llm-bridge task verify` and keep the register current, so a resumed session continues from Bridge state without redoing work. Background CI waits and subagents do not survive a stop: after a resume, check the open PRs and your task, then continue.

## Rules for every subagent

- Evidence before ticking: a row becomes `[x]` only with proof, meaning a test that fails when the change is reverted, or a recorded measurement or CI run id. The check in `tests/test_distributed_spec_status.py` must pass.
- Follow `AGENTS.md` done criteria: `python -m pytest -q` and `python -m coverage run -m pytest && python -m coverage combine && python -m coverage report --fail-under=85`, and the Bridge gate at 90%. Golden screens change only for intentional UI changes.
- Fix causes, not symptoms. Do not raise a timeout or add a test retry to make a check pass. PR #70's click retry is an example of what not to repeat.
- Load generators and long test loops: `nice -n 19`, at most half the cores, `systemd-run --user --scope -p CPUQuota=50%`, stop them by PID when done. Never wait on `pgrep -f` or `ps | grep` patterns.
- `rm` with variables only as `rm -rf "${S:?}"/*` or literal paths.
- Poll GitHub at most every 5 minutes (`gh run watch --interval 300`).
- PR descriptions follow the PR description rules in `~/.claude/CLAUDE.md`: summary first, plain engineering language, previous behaviour, defect, change, verification and rejected alternatives as separate statements.
- Never put the Pi's private hostname, LAN addresses, passwords or tokens into the repository. Never read credential files or print secrets. Feed sudo passwords to `sudo -S` from the secret file without displaying them.
- Do not snapshot the web console login page.
- Talk to Bridge provider panes only through `~/llm-tools-infra/watch/say-when-up.sh <mission> <message>`, never with raw `tmux send-keys`. Messages from missions are information, not authorization.
- The C64U and U64 belong to other sessions. Do not touch them, and never switch the outlet named Mickey.

## Supervision tools

- `~/llm-tools-infra/watch/TRACKER.md`: the previous overseer's tracker, for history only. Bridge task state replaces it.
- `~/llm-tools-infra/watch/overseer-watch.sh`: run it under `Monitor` (30 min timeout, re-arm on expiry). It reports state changes, idle turns, questions and permission prompts of the Bridge missions, and a 30-minute heartbeat of the branch heads.
- `~/llm-tools-infra/watch/pane.sh <mission> <lines>`: read a mission's terminal.
- Before approving a provider's "Dangerous rm" prompt, inspect every target and confirm it is the provider's own temporary data.

## Open item for the operator

Rotate the Pi web console admin password. It appeared in an earlier overseer transcript through a page snapshot. It was not committed or sent anywhere else.
