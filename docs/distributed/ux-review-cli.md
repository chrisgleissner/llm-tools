# llm-bridge CLI and terminal console: UX review (PRD-031)

Status of this register: the findings CLIUX-001 to CLIUX-050 come from the overseer's expert pre-review of 2026-11-06, copied unchanged below. An independent second reviewer extends them as CLIUX-051 onwards (section "Second reviewer"). Each finding is fixed under PRD-033 with a test that fails before the fix; the "Fix status" table records the commit and the test for each finding.

Reviewed: `llm-bridge` 1.4.0 (main, `b421a58`), 2026-11-06.

Method: every `--help` page and help topic was captured. The first run was driven with an empty state directory, no llm-tools config, a fake `HOME`, and a fake `claude` (the one from `tests/test_bridge_first_run_path.py`) plus fake, signed-out `codex` and `opencode`. The home screen, new-mission form, station, console views F1 to F9, overlays, composer, Ctrl-K, the approval dialog, detach and exit were driven in a private tmux server at 100x30, 60x18 and 40x12. Headless and Background missions were driven from the CLI with `--command-template`. The 80x24 ASCII and UTF-8 golden screens in `tests/golden/screens/` were read for the narrow and ASCII layouts. No real provider was started, and no real mission or coordinator was touched.

## Summary

### Overall verdict

A novice can get from install to a completed first mission without reading code, but only on the path the README walks through: home screen, `N`, five Tabs, Start, `/exit`, `Ctrl-]` `B`, `/approve`, `y`. That path worked end to end in this review (fake provider, about 70 s including the approval). The help text for that path matches what the screen does, and the `doctor` `Next` line, the Start-row cost hint and the approval dialog are well judged.

Away from that path the product is not yet ready for thousands of users. The main problems are:

* The CLI reports success when nothing will run.
* Input is accepted that can only fail later.
* The same state has three or four names depending on the surface.
* Internal telemetry and Python values (`None`, `[Errno 2]`, `boottime_seconds=…`) reach the user.
* Several help statements contradict observed behaviour.

The command surface has grown by accretion: 35 top-level commands (including internal ones), six overlapping ways to stop work, two list commands, two console commands, and three ways to set the run mode. None of this is hard to fix, but it needs one consistency pass across vocabulary, help, errors and JSON before a public release.

### Count by severity

| Severity | Count |
| --- | --- |
| Critical | 2 |
| High | 8 |
| Medium | 25 |
| Low | 15 |
| Total | 50 |

### Top 10 issues

1. **CLIUX-001 (critical)**: `start` prints a "running" receipt and exits 0 for a cancelled mission and for a mission with an invalid provider. Neither one runs.
2. **CLIUX-002 (critical)**: `mission create --providers claud` is accepted. The mission is saved and then fails at start. `rotation --set` validates the same input.
3. **CLIUX-003 (high)**: There is no way to see what the provider did before you approve. The approval dialog says "read the provider output before approving" but links to nothing, and `task show` and `status` show no output, log or diff.
4. **CLIUX-004 (high)**: `llm-bridge usage` and `llm-bridge control` crash with a Python traceback when there is no terminal.
5. **CLIUX-005 (high)**: The Usage view (F8) says Claude is `unavailable`, `Ready no`, while Overview, Providers and `status` say `80% left · usable` in the same session.
6. **CLIUX-006 (high)**: One state has several names. A task awaiting approval shows as `VERIFYING` (CLI, console), `REVIEW` (home, picker, theme), `completed` (Agents view, Activity) and "awaits approval" / "your review" (prose). The mission header meanwhile says `● RUNNING`.
7. **CLIUX-007 (high)**: A mistyped command prints the full argparse choice list, including `_controller` and `_station-gc`, and offers no suggestion. The console's command line does suggest a correction.
8. **CLIUX-008 (high)**: The run mode is chosen with two boolean flags across three modes. The CLI default (Background) differs from the home-screen default (Native), and the home form cannot select Background.
9. **CLIUX-009 (high)**: Help contradicts behaviour in four places: the default rotation, whether a completed mission accepts tasks, what happens to the command line after an error, and the F-key list.
10. **CLIUX-010 (high)**: A second mission created in the same folder is not flagged. Folder inference then fails, and the `Needs you` line suggests a command that fails in that folder.

---

## Findings

### CLIUX-001 · critical · CLI feedback · `start` reports success when nothing will run

**Command**

```
llm-bridge cancel --mission e14f        # Cancel requested · mission e14ff4b7 is CANCELLED
llm-bridge start --mission e14f </dev/null
```

**Evidence**

```
mission e14ff4b7 running without a terminal station · inspect with: llm-bridge status --mission e14ff4b7 · controller log: …/controller.log
EXIT 0
$ llm-bridge status --mission e14f
Mission     e14ff4b7 "Write tests for the parser" · CANCELLED (headless)
Controller  finished
```

The same happens with an invalid provider (see CLIUX-002):

```
mission 4f9ebda7 running · attach with: llm-bridge attach --mission 4f9ebda7 · when the provider finishes, …
EXIT 0
$ llm-bridge status --mission 4f9e
Mission     4f9ebda7 "z" · FAILED (background)
Controller  failed — invalid provider in --providers: claud
```

`resume` on the same cancelled mission is refused correctly: `mission e14ff4b7 was cancelled and cannot be resumed`.

**Why it hurts users**: The receipt is the only feedback a script or a novice gets. A cron job or CI step that runs `start` sees exit 0 and the word "running", then waits for work that never happens. This breaks trust in every other receipt.

**Fix**: Apply the same precondition check `resume` already uses. A cancelled mission should give `llm-bridge: mission e14ff4b7 was cancelled and cannot be started; create a new mission with …` and exit non-zero. For a controller that fails during startup, `start` should wait for the controller-ready handshake (the `controller-ready.json` file already exists) and print the failure reason with a non-zero exit, for example `llm-bridge: mission 4f9ebda7 could not start: unknown provider 'claud' in its rotation · fix it with: llm-bridge rotation --mission 4f9ebda7 --set claude`. Print "running" only once the controller has reported ready.

### CLIUX-002 · critical · Error prevention · Provider names are not validated at creation

**Command**: `llm-bridge mission create --prompt "z" --providers claud`

**Evidence**

```
Created mission 4f9ebda7 "z" · task 1 · teams on · token spend medium · start with: llm-bridge start --mission 4f9ebda7
EXIT 0
$ llm-bridge rotation --mission 4f9e
1  claud  claud  provider default
```

The same mistake in `rotation` is caught:

```
$ llm-bridge rotation --mission 14f5 --set claude,nosuch
llm-bridge: unknown rotation entry 'nosuch' · available: claude, codex, copilot, kilo, minimax, opencode, zai
```

Defaults are not checked either. Without a config, the default rotation is `claude, codex, opencode`, but `doctor` reports `codex: installed, not logged in` and `opencode: installed, not logged in`. `mission create` says nothing about this.

**Why it hurts users**: The mistake surfaces later, as a FAILED mission whose `Needs you` line points at `events` rather than at the fix.

**Fix**: Run the `rotation --set` validator in `mission create` and `start --providers/--routes`, and add a did-you-mean suggestion (`unknown provider 'claud' · did you mean claude?`). Warn, without failing, when a rotation entry is installed but not signed in: `warning: codex is not signed in (run: codex login); it will be skipped until it is`.

### CLIUX-003 · high · Verification · No way to see the provider's work before approving

**Screens and commands**: the approval dialog, `task show`, `status`, Tasks view Enter (inspect).

**Evidence**

Approval dialog:

```
Approve verification and mark task 1 complete? · mission f7ae7f30
Evidence: none recorded; read the provider output before approving.
Last run: claude, exit 0, exited
Workspace: changed during the run
Y approve · N or Esc cancel
```

`task show --task 1` prints only the title, priority, order, revision, evidence and prompt. Its JSON keys are `acceptance, created_at, depends_on, …, prompt, proposal_status, revision, status, title`, with no operations, log path or diff. The Tasks-view inspect pane shows `Amendment history`, `proposal NONE | proposed by None` and the prompt, but no output. For a headless or background mission there is no station either. The log path appears only as a truncated `logs_path=/tmp/…/state/ru…` field in `events`.

**Why it hurts users**: The product promise is "You approve, not the exit code." The approve step is the moment the user most needs evidence, and the UI tells them to read output it does not show. In practice users will approve blind.

**Fix**

* Add `llm-bridge task output --task N [--follow]`, or `task show --output`, that pages the retained provider log of the latest operation through the existing viewer.
* Add `llm-bridge task diff --task N`, which runs `git diff` from the repository checkpoint the controller already records (`repository-checkpoint` event) to the current tree.
* In the approval dialog, list the changed files (`Workspace: 1 file changed: hello.txt (+1)`) and add `o output · d diff` keys.
* In the Tasks inspect pane, add "Last run: claude · exit 0 · 12 s · o output · d diff".

### CLIUX-004 · high · Robustness · `usage` and `control` crash without a terminal

**Command**: `llm-bridge usage --mission 14f5 </dev/null` (the same for `control`)

**Evidence**

```
Traceback (most recent call last):
  File "/usr/lib/python3.14/curses/__init__.py", line 78, in wrapper
    cbreak()
_curses.error: cbreak() returned ERR
…
  File "…/llm_tools/bridge_tui.py", line 6197, in run_control
EXIT 1
```

`missions --pick` handles the same case correctly: `llm-bridge: missions --pick needs an interactive terminal; use llm-bridge missions to list`. `view --file` without a terminal prints the file, which is also good.

**Why it hurts users**: Tracebacks look like bugs and end up in issue trackers. Scripts and SSH sessions without `-t` hit this.

**Fix**: Run the same TTY guard as `missions --pick` before curses starts. For `usage`, fall back to printing the `llm-usage` table, as `view` falls back to printing the file. For `control`, print `llm-bridge: control needs an interactive terminal; use llm-bridge status`.

### CLIUX-005 · high · Status consistency · Usage view contradicts the other capacity displays

**Screen**: console F8 Usage, compared with F1 Overview, F3 Providers and `llm-bridge status`, in the same session.

**Evidence**

```
F1  Capacity    ■ claude (last selected) · 5h 80% left · weekly 80% left
F3  ■ claude  default model  ○ ready / 5h 80% left / weekly 80% left
F8  Claude     no      5h           unavailable
                       weekly       unavailable
    Codex      no      5h           unavailable
    Copilot    no      monthly        0% ░░░░░░░░░░   ↓ conserve            24d 5h
    Kilo       no      balance      unavailable
    MiniMax    no      5h           unavailable
    OpenCode   no      balance      unavailable
    Z.ai       no      5h           unavailable
```

The rotation is `claude` only. Kilo and MiniMax were reported as "not installed" by the new-mission form.

**Why it hurts users**: Two screens in the same window give opposite answers to "can my provider run?". The F8 view also lists seven providers, most irrelevant to the mission, so the relevant row is hard to find.

**Fix**

* Feed F8 from the same capacity snapshot the controller uses, or label the two sources explicitly: `Usage · live llm-usage read · differs from the controller's last observation (80% left, 38 s ago)`.
* Limit F8 to the mission's rotation by default, with `a` to show all providers.
* Hide providers that are not installed, which the enabled-providers config already supports.
* A `0%` row should read `× empty`, not `↓ conserve`.

### CLIUX-006 · high · Vocabulary · One state, many names

**Screens**: home list, mission picker, console header, Tasks, Agents, Activity, `theme`, `status`.

**Evidence** (the same moment: task 1 awaiting approval)

```
Home list         ◆ REVIEW     14f56611  ▌ Write tests for the parser  0/1 done
Console header    LLM BRIDGE · ▌ Write tests … · ● RUNNING
Console line 2    task 1 awaits approval · type /approve 1
Tasks             1  ◆ VERIFYING  P0  …
Agents (F9)       Create a file named hello.txt · claude · 0m · completed · cfg:unknown/cfg:unknown
Activity          Provider result recorded · completed
theme             state_review      ◆ REVIEW     verifying, or needs your decision
Station strip     repo  Task 1 ● RUNNING   (for about 10 s after the provider exited; see CLIUX-022)
```

After adding a task to a COMPLETED mission, the mission becomes `STOPPED`, not `CREATED` or `READY`. The hint shown was `Mission reopened · start it with: llm-bridge start`.

The command to approve is `task verify --approve`. The command to reject is `task reject`. The console verbs are `/approve` and `/reject`.

**Why it hurts users**: Users learn a state machine through its names. `VERIFYING` suggests that Bridge is doing something and the user should wait. `REVIEW` correctly says the user must act. `completed` (Agents view) says it is finished. Users will miss the approval step or approve too early.

**Fix**

* Choose one user-facing name, preferably `NEEDS REVIEW` (or `REVIEW`), and use it in the CLI tables, console, JSON `status_label` and help. Keep `VERIFYING` only as the JSON enum value if compatibility requires it.
* When any task needs review, make the mission header show `◆ NEEDS YOU`, not `● RUNNING`.
* The Agents view and the Activity line should say `exited 0 · awaiting review`, not `completed`.
* Add `task approve` as the CLI verb, pairing with `task reject`. Keep `task verify --evidence` for evidence only, and keep `--approve` as a deprecated alias.
* A reopened mission should show `READY` (or `CREATED`), not `STOPPED`.

### CLIUX-007 · high · Error recovery · Unknown command leaks internal commands and offers no suggestion

**Command**: `llm-bridge stauts`

**Evidence**

```
usage: llm-bridge [COMMAND ...]
llm-bridge: error: argument COMMAND: invalid choice: 'stauts' (choose from help, view, mission, status, agents, suspend-until-credits, task, proposal, events, start, pause, resume, continue, stop, cancel, attach, detach, missions, gc, backup, restore, worker, remote, finish, doctor, theme, console, control, usage, station, rotation, steer, _station-gc, _controller)
```

`task lst` prints the whole subcommand set twice, once in the usage line and once in the error. The console's command line handles the same mistake well: `error: Unknown command 'aprove' · did you mean approve? · /help lists commands`.

**Why it hurts users**: Typos are the most common CLI error. A 35-word list is not a recovery path. Exposing `_controller` invites users to run it.

**Fix**: Override `ArgumentParser.error` for unknown choices: `llm-bridge: unknown command 'stauts' · did you mean status? · llm-bridge --help lists commands`. Reuse the console's `difflib` matcher, and exit 2. Hide `_controller` and `_station-gc` with `argparse.SUPPRESS`, and do the same for `console` (see CLIUX-033). Apply the same handling to unknown options (`--misison` currently gives `unrecognized arguments: --misison 14f5` with no suggestion).

### CLIUX-008 · high · Information architecture · Run mode is set by two flags with an implicit third value

**Commands**: `mission create --help`, `start --help`, `help quickstart`, the home form.

**Evidence**

```
--headless   Headless mode: … without --headless or --native the mission runs in Background mode (a station shows the activity log)
--native     Native mode: the provider's own interactive terminal runs in a station
Defaults: … scripted mode is Background unless --native/--headless is explicit.
```

The home form's Mode field cycles between `Native — provider terminal in a station` and `Headless`. Background is not offered. `help quickstart` says: "The home form offers Native (recommended) and Headless. CLI defaults and saved modes remain compatible." `--native --headless` together is rejected with a long explanation. JSON exposes the mode as two booleans, `"headless": true, "native_tui": false`.

**Why it hurts users**: There are three modes but two switches. The default differs between the two entry points. The home form cannot create the CLI's default mode. "Remain compatible" is a note for maintainers, not for users. A user who follows the README script (`--native`) and then drops the flag silently gets a different mode.

**Fix**: Add `--mode native|background|headless` to `mission create` and `start`, keep `--native`/`--headless` as hidden aliases, and use `native` as the default everywhere, matching the home screen recommendation. If Background must stay the scripted default, state why in one sentence and offer Background in the home form. In JSON, add `"mode": "native"`.

### CLIUX-009 · high · Help accuracy · Help contradicts observed behaviour

**Evidence**

1. `mission create --help`: `Defaults: … routes claude,codex` and `--providers LIST … (default: claude,codex)`. `help quickstart`: "without such a config it uses the built-in claude,codex,opencode". With no config, `rotation` shows `claude, codex, opencode`.
2. `help states`: "Completed missions have no active execution and cannot accept another task." In practice, `task add --mission f7ae7f30 --prompt "…"` on a completed mission gives `Added task 2 … Mission reopened`. Inside the folder without `--mission`, the same command is refused: `mission f7ae7f30 … is finished (completed); add follow-up work with: llm-bridge task add --mission f7ae7f30 …`.
3. `help keys`: "After a failed command the line stays empty; Up recalls it." In practice, after `/reject 1` failed with `error: usage: reject [TASK-NUMBER] REASON`, the line still held `/reject 1`. Typing `/cancel` then produced the confirmation `Send task 1 back for another attempt? Reason: 1/cancel` (see CLIUX-011).
4. `help keys`: "F1-F9 select Overview, Tasks, Providers, Activity, Sessions, Health, Actions, Usage." That is eight names for nine keys; Agents (F9) is missing. The typed view names likewise omit `tasks` and `agents`.
5. `--routes LIST … (default: claude,codex)`: routes and providers are given the same default list.

**Why it hurts users**: Users who read help are the careful ones. Each contradiction teaches them not to trust it.

**Fix**: Correct each statement. Generate default values in help from the same function that resolves them (`config.enabled_providers` / rotation resolution) instead of literal strings. Add a test that renders `mission create --help` with no config and compares the printed default with the resolved rotation. Decide the completed-mission rule once: either always allow reopening with a clear receipt, or refuse in both paths.

### CLIUX-010 · high · Error prevention · Duplicate missions in one folder break folder inference

**Commands**: `mission create --prompt "Write tests for the parser"` run twice in the same folder, then `status`.

**Evidence**

```
Created mission 14f56611 "Write tests for the parser" …
Created mission e14ff4b7 "Write tests for the parser" …       (no warning)
$ llm-bridge status
llm-bridge: several missions match folder …/repo2; pass --mission ID:
  e14ff4b7  Write tests for the parser  CREATED
  14f56611  Write tests for the parser  CREATED
$ llm-bridge status --mission 14f5
Needs you   task 1 awaits approval · llm-bridge task verify --task 1 --approve (or reject: …)
$ llm-bridge task verify --task 1 --approve
llm-bridge: several missions match folder …/repo2; pass --mission ID:
```

**Why it hurts users**: Double-submitting is common (shell history, retries after an unclear receipt). The two missions cannot be told apart by name, and the copy-paste command the tool itself suggests fails.

**Fix**

* On create, when an unfinished mission already exists for the folder, print `warning: this folder already has unfinished mission 14f56611 "Write tests for the parser" (CREATED); llm-bridge task add adds to it`. Offer `--new` to suppress the warning; in a TTY, ask.
* Make every suggested command include `--mission <prefix>` whenever folder inference is ambiguous. Simplest: always include it in `Needs you` and receipts.
* Add a CREATED/UPDATED time column to the ambiguity list.

### CLIUX-011 · medium · Console command line · Failed command text stays and is extended by the next input

**Screen**: console command line.

**Evidence**

```
› /reject 1
error: usage: reject [TASK-NUMBER] REASON
(typed) /cancel  ⏎
Confirm
Send task 1 back for another attempt? Reason: 1/cancel · mission 14f56611
```

**Why it hurts users**: The user intended to cancel the mission and was shown a confirmation to reject a task with a nonsense reason. The confirmation dialog prevented harm, but a quick `y` would have recorded it. The parser also accepted `1/cancel` as a reason after treating `1` as a task number, which shows that the argument split is fragile.

**Fix**: Do what `help keys` says: clear the line after a failed command and keep it in history for Up. Also reject a reason that begins with `/`.

### CLIUX-012 · medium · Discoverability · Slash completion is silent and picks the first match

**Screen**: console command line.

**Evidence**: Typing `/` shows nothing beyond `Enter run · Tab complete · …`. Typing `/s` and pressing Tab gives `› /status`, although `/start`, `/stop`, `/steer`, `/station`, `/sessions` and `/suspend-until-credits` also match.

**Why it hurts users**: The help says the command line behaves "like Codex and Claude Code". Both of those show a filtered list as soon as `/` is typed. Silent first-match completion of an ambiguous prefix can turn `/st` into the wrong command. Today it completes to `/status` (harmless), but a different first match could be `/stop`.

**Fix**: On `/`, show a popup list above the prompt with each command's one-line description (the data already exists in the Actions registry), filtered as the user types. Tab should complete only the common prefix when there are several matches, and a second Tab should show them.

### CLIUX-013 · medium · Composer · "Esc Recover" saves a file that is never offered back

**Screen**: Ctrl-E composer.

**Evidence**: The composer footer reads `F2/Ctrl-S Save · Esc Recover · Ctrl-D Discard · 0/1048576 bytes · 1 lines`. After typing "Add a fuzz test" and pressing Esc, the status line read `Task source retained for recovery: /tmp/…/state/drafts/composer-recovery-ph…`, with the path truncated. Ctrl-E then opened an empty composer.

**Why it hurts users**: "Recover" as an Esc label is opaque. The draft is preserved somewhere the user cannot see in full, and the natural action (Ctrl-E again) does not bring it back.

**Fix**: Label Esc `Esc close (draft kept)`. Reopen the last draft on Ctrl-E with `Restored draft from 18:42 · Ctrl-D discard`. Drop the byte counter in favour of a line count, or show bytes only above 80% of the limit, and fix the plural "1 lines".

### CLIUX-014 · medium · Keyboard model · The same key does different, sometimes destructive, things by focus

**Evidence** (from `help keys`, the composer footer and the view footers)

* `Ctrl-D`: exits the console, but in the composer it means "Discard".
* `F2`: switches to the Tasks view, but in the composer it means "Save".
* `Ctrl-E`: adds a task in the body, but in command input it moves to the end of the line ("Ctrl-E in command input means End").
* `Ctrl-K`: opens Actions, while in readline it means kill to end of line.
* `Ctrl-R`: amends a task, while in readline it means reverse search.
* Printable text in the body starts a command, but `o`, `v`, `l` and `i` are single-key actions in some views.

**Why it hurts users**: Mode errors are the most common source of lost work in TUIs. A user who presses Ctrl-D to leave the composer, as they would leave the console, gets a discard prompt. A user who presses Ctrl-E at the prompt to reach the end of a long command gets the end of the line in one focus and a new task editor in the other.

**Fix**: Pick one meaning per key across all modes. Suggested: Ctrl-S saves everywhere, and F-keys only switch views (so F2 never means Save). Ctrl-D only exits or closes and never discards; use Ctrl-X or a confirm for discard. Use `n` (body) or `/add` for a new task instead of Ctrl-E. Document the remaining exceptions in one table in `help keys`.

### CLIUX-015 · medium · Machine output · `--json` is not JSON on errors, and envelopes differ

**Evidence**

```
$ llm-bridge status --mission zzzz --json
llm-bridge: no mission matches 'zzzz'; run llm-bridge mission list      (plain text, exit 2)
$ llm-bridge task list --json        → [ {...} ]                    (bare array)
$ llm-bridge mission list --json     → { "missions": [ ... ] }       (object)
```

Task objects carry `"id": 2` (global) and `"ordinal": 1` (the number users type). `mission create --json` returns `"initial_task_id": 3` while the human receipt says `task 1`. Config keys differ from flag names: `native_tui`/`headless` versus `--native`/`--headless`, and `team_policy_enabled` versus `--teams`.

**Why it hurts users**: Scripts must parse stderr text to tell errors apart. Users who copy the `id` field into `--task` act on the wrong task, or on none.

**Fix**: With `--json`, write errors to stdout as `{"error": {"code": "mission-not-found", "message": "…", "hint": "…"}}` and keep the non-zero exit. Wrap every list in an object (`{"tasks": [...], "total": N}`). Rename or alias `ordinal` to `number`, which is what users type, and document `id` as internal. Expose `mode` (CLIUX-008). Publish the error codes.

### CLIUX-016 · medium · Exit codes · Everything is 2, and no-op outcomes are inconsistent

**Evidence**

| Situation | Exit |
| --- | --- |
| argparse usage error | 2 |
| no mission for folder / no match / prefix too short | 2 |
| wrong state (`task verify` on a queued task) | 2 |
| `finish` on a headless mission | 2 |
| `pause` when nothing is running (`Nothing to pause …`) | 0 |
| `stop` when already stopped (`Nothing to stop …`) | 0 |
| `start` on a cancelled mission | 0 (CLIUX-001) |

No exit codes are documented in `--help`, `help recovery` or `docs/llm-bridge/operations.md`.

**Why it hurts users**: Scripts cannot tell "you typed it wrong" from "that mission does not exist" from "not in that state". The no-op cases are inconsistent: `finish` on the wrong mode fails, while `pause` on the wrong state succeeds.

**Fix**: Document a small set: 0 success or no-op, 1 runtime failure, 2 usage error, 3 not found or ambiguous, 4 wrong state. Use them consistently, and list them in `llm-bridge --help` under "Exit status".

### CLIUX-017 · medium · Help · Boilerplate on every page hides the content, and the side-effect line is sometimes wrong

**Evidence** (the same on almost every subcommand page)

```
Does not directly start paid provider work.
Use the actual mission ID and task number; input files retain source text.
Example:
  llm-bridge task pause --mission ID --task 1
  llm-bridge task pause --mission 3f9a1c2b --task 1
--mission takes a unique ID prefix (4+ characters) and may be omitted inside the mission's folder.
More: llm-bridge help keys · llm-bridge help recovery
```

The side-effect line is wrong where it matters most:

* `continue --help`: "Start the next task now … Does not directly start paid provider work."
* `suspend-until-credits --help`: "Arm a wake timer and suspend after a fresh capacity and safety check. Does not directly start paid provider work." This omits that the command suspends the machine.

**Why it hurts users**: Users stop reading repeated lines, so the one page where the line is false (or where the real side effect is a machine suspend) gets no attention. Two examples that differ only in a placeholder add nothing.

**Fix**: Replace the line with a short `Effects:` line that is specific per command. Examples:

* `start`: `Effects: starts provider work (uses paid capacity).`
* `suspend-until-credits`: `Effects: suspends this computer until credits recover.`
* `status`: no line, because read-only is the default assumption.

Keep one example per page that shows a realistic use. Move the `--mission` prefix note into the `--mission` option text, where it already is. Drop "input files retain source text".

### CLIUX-018 · medium · Discoverability · Top-level `--help` is argparse boilerplate plus an unordered wall

**Evidence**

```
usage: llm-bridge [COMMAND ...]
positional arguments:
  COMMAND
options:
  -h, --help  show this help message and exit
…
Also: missions, finish, mission list, mission colour, rotation, theme, resume, cancel,
detach, proposal, events, doctor, control, usage, station, steer, view, backup, restore,
remote, worker, help.
More: llm-bridge COMMAND --help · help keys · help distributed (--target WORKER COMMAND)
```

The page does not list `--version`, `--target` (a global option, mentioned only in the "More" line), `suspend-until-credits`, `continue`, `agents` or `gc`, all of which are public commands with their own help pages.

**Why it hurts users**: `positional arguments: COMMAND` is noise. The "Also" line mixes subcommands (`mission list`) with top-level commands, and life-cycle commands with maintenance ones, so `cancel` sits between `resume` and `detach`. Experts cannot scan it, and novices do not know which commands matter.

**Fix**: Use a custom usage text grouped like `gh`:

* Start here: home, doctor, help quickstart.
* Missions: mission create/list/set, missions, status.
* Tasks: task add/list/show/amend/approve/reject.
* Run control: start, pause, resume, continue, finish, stop, cancel, steer.
* Station: attach, detach, station, control.
* Capacity and power: usage, rotation, suspend-until-credits.
* Data: events, backup, restore, gc.
* Distributed: remote, worker.
* Appearance: theme, mission colour.

Add a "Global options" section listing `--version` and `--target WORKER`. Each line should carry its one-line description.

### CLIUX-019 · medium · Help consistency · Subcommand listings are unordered, duplicated or missing descriptions

**Evidence**

```
$ llm-bridge mission --help
    create              create a mission and its first task
    set                 change a mission setting
    colour (color)      pin the mission's identity colour (1-8) or return to the automatic one
    list                List saved missions; no state changes or provider work
    color               color
```

`task --help` lists `priority, reorder, dependencies, pause, resume, cancel, retry, verify, reject, add, list, show, amend`, with the most common commands last and descriptions alternating between lowercase and Capitalised. `mission set`, `mission colour`, `theme`, `backup`, `restore`, `remote` and `worker` have no description line and no example.

**Fix**: Register subparsers in a fixed order, from common to rare (create, list, set, colour). Suppress the duplicate alias row. Use one case style for descriptions (lowercase, as argparse does). Give every page a description and one example.

### CLIUX-020 · medium · Information hierarchy · Internal telemetry is shown as user information

**Evidence**

`llm-bridge events` (human output):

```
53  11-06 18:39:41  controller-mode     actual_mode=headless boottime_seconds=198108.113086146 controller_pid=2844186 monotonic_seconds=137…
54  11-06 18:39:41  controller-heartbeat  controller_state=ACTIVE desired_mission_status=RUNNING observed_at=… owner_…
46  11-06 18:39:41  command-queued      command=task.verify command_id=24c5a45674eb4601bb3d1f342400e8e6
47  11-06 18:39:41  command-rejected    command_id=24c5a45674eb4601bb3d1f342400e8e6
```

Console views:

```
Activity  18:37:55  Prompt assembled · assembled_bytes=3383 bridge_added_bytes=1412 incoming_bytes=1971 ope…
Activity  18:37:55  Shared wake plan updated
Activity  18:38:07  Session unavailable · claude      (shown twice)
Health    Controller heartbeat  active · 3s ago · owner lock held · mission wants RUNNING
Health    Persistence           sqlite schema 10 · wal journal
Health    Capacity              persisted runtime event (38s old) · selected claude · has capacity
Sessions  e7e9a36a  1  claude  -  EXITED  -  UNAVAILABLE
Overview (golden)  Policy      legacy (unchanged)
Task inspect  Origin operator | proposal NONE | proposed by None
              revision 2 APPLIED: {"prompt": "Write parser tests incl. edge cases"}
Agent inspector (golden)  role: unknown (unknown) · mission: None · last observed: unknown · native status: unknown
```

**Why it hurts users**: The Activity view, the CLI `events` and the inspectors are where users go to understand a failure. Byte counts, monotonic clocks and enum names bury the one line that matters. `command-rejected` records no reason at all.

**Fix**

* Give `events` the same humaniser the Activity view uses (`bridge_tui._HUMANIZERS`). Hide `controller-heartbeat`, `controller-mode`, `capacity-wake-plan` and `prompt-assembled` unless `--verbose` is given, and keep the raw fields for `--json`.
* Add the reason to `command-rejected` (`task 1 is queued, not awaiting verification`).
* In Health, move the schema, journal mode and owner lock under an "Internals" fold.
* In Sessions, map `UNAVAILABLE` and `NATIVE_CREATED` to plain words ("cannot resume", "resumable").
* In inspectors, omit fields that are `unknown` or `None` and show a single `Not reported by the provider` line instead.
* Show amendments as text diffs, not JSON.
* Rename `Policy legacy (unchanged)` to `Policy defaults from before 1.3 (teams/token spend not set)`.

### CLIUX-021 · medium · Error messages · Python values and errno strings reach the user

**Evidence**

```
$ llm-bridge mission create --file nosuch.md
llm-bridge: [Errno 2] No such file or directory: 'nosuch.md'
$ llm-bridge steer --mission 14f5 --kind handoff
llm-bridge: handoff station is not configured for this mission: None
$ echo -n "" | llm-bridge mission create --file -
llm-bridge: mission name and objective must be non-empty
```

**Fix**

* `llm-bridge: cannot read --file nosuch.md: no such file`.
* `llm-bridge: --kind handoff needs --route NAME (one of: claude)`.
* `llm-bridge: the prompt is empty (read from standard input)`.

Add a test that greps all `BridgeError` messages for `None`, `Errno` and `{`.

### CLIUX-022 · medium · System status · The station strip lags, and its key message is truncated

**Screen**: station at 100 columns, immediately after the provider exits.

**Evidence**

```
Provider exited (status 0) · Task 1 is waiting for your review. Ctrl-] then B, then /approve or /rej
Bridge: Ctrl-] then B │              repo  Task 1 ● RUNNING             │ ▶ Provider: Ctrl-] then P
```

The strip said `● RUNNING` for about 10 s before changing to `◆ VERIFYING`. The message line is cut at `/rej` at 100 columns, and at 80 columns it loses more.

**Why it hurts users**: The two lines contradict each other at the moment the user is deciding what to do, and the instruction is cut off.

**Fix**: Update the strip from the same event that writes the message. Shorten the message so it fits in 80 columns: `Task 1 ready for review · Ctrl-] B then /approve`. Put the alternative (`/reject`) in the controls.

### CLIUX-023 · medium · Navigation model · Two near-identical control surfaces and a double exit

**Screens**: controls inside the station (Ctrl-] B), then the controls that open in the user's own terminal after `Ctrl-] d`.

**Evidence**: Both screens render the same header, tabs and Overview. After detach the status line shows `saved as f7ae7f30`, which is stale text from mission creation. A second Ctrl-D is then needed, and the shell receives:

```
Detached from mission f7ae7f30 (Create a file named hello.txt containing the word hello, then stop.)
 · the mission is completed
```

**Why it hurts users**: Users cannot tell whether they are inside the station or outside, and so cannot tell whether Ctrl-] keys will work. "Detached" from a completed mission is odd, and the wrapped line starts with ` · `.

**Fix**: Mark the outside controls clearly, for example a header badge `outside station · a attach`. Clear transient messages on mode change. On a completed mission, `Ctrl-] d` should exit straight to the shell with `Mission f7ae7f30 completed · 1/1 tasks`. Print the exit message as one line truncated to the terminal width.

### CLIUX-024 · medium · Concepts · Six overlapping ways to stop work, with different confirmation rules

**Commands**: `pause`, `stop`, `stop --force`, `cancel`, `finish`, `task pause`, `steer --kind pause-after-iteration`, `steer --kind stop-after-task`, `continue`.

**Evidence**

* `pause`: "Hold after the current invocation; never suspend the host."
* `stop`: "Stop the owned controller; --force targets owned running work."
* `cancel`: "Cancel this mission's work; unrelated sessions are unaffected."
* `finish`: "Ask the native provider to exit (its own /exit or /quit)."

`llm-bridge cancel --mission e14f` cancels immediately with no prompt, while `/cancel` in the console asks. Neither says the action is irreversible, though `resume` later refuses with `was cancelled and cannot be resumed`. The console dialog reads `Cancel this mission and its unfinished tasks? … Y confirm · N or Esc cancel`, so "cancel" means two different things in one line. `pause` twice prints `Pause requested · mission 14f56611 is PAUSED` both times, without saying it was already paused.

**Why it hurts users**: Users have to learn controller, invocation, boundary and owned work to pick the right verb. The irreversible verb has the lightest guard on the CLI.

**Fix**

* In `help quickstart`, add a single table "Stopping work" with columns for what happens to the provider, whether it can be undone, and the command.
* Make `cancel` ask for confirmation on a TTY, and require `--yes` otherwise. Say `This cannot be undone` in both places.
* Use `Keep mission` / `Cancel mission` as the dialog buttons.
* Make repeated `pause`/`stop` say `already paused`.
* Fold the `steer` pause and stop kinds into `pause --after-task` and `stop --after-task`.

### CLIUX-025 · medium · Feedback · Rejecting a task starts a new paid attempt without saying so

**Command**: `llm-bridge task reject --mission 14f5 --task 1 --reason "needs edge cases"`

**Evidence**: The command printed `Sent back · task 1 "Write tests for the parser" · QUEUED · reason recorded`. Four seconds later, `status` showed the task back in `VERIFYING`: the controller had already started and finished a new provider run. The `reject` help says only "send a task awaiting verification back for another attempt".

**Why it hurts users**: Users reject in order to think, edit the prompt, or switch provider. The attempt starts immediately and uses paid capacity.

**Fix**: Make the receipt explicit: `Sent back · task 1 will run again on claude now (uses capacity) · to hold it instead: llm-bridge task pause --task 1`. Consider `--hold` on reject, and in the console offer `Reject and hold` alongside `Reject and retry`.

### CLIUX-026 · medium · Error prevention · Amending a task that awaits review changes what will be approved

**Command**: `task amend --task 1 --prompt "Write parser tests incl. edge cases"` while task 1 is `VERIFYING`.

**Evidence**: `Amended · task 1 is now revision 2`. The task stays `VERIFYING`, the Tasks view shows `Revision 2` and the new prompt, and the approve dialog would mark it complete. The work under review was produced for revision 1. The title stays `Write tests for the parser`.

**Why it hurts users**: Approving now records revision 2 as done although no provider ever saw it.

**Fix**: When amending a task in review, either reject it implicitly (`Amended · task 1 revision 2 will run next; the revision 1 result is no longer awaiting review`) or refuse with `task 1 is awaiting review; approve or reject it first, or pass --requeue`. Update the title when it was derived from the first prompt line.

### CLIUX-027 · medium · Help viewer · The help pager shows file-viewer controls and wraps mid-word

**Screens**: `?` and `/help` in the console; `?` on the home screen.

**Evidence**

```
Bridge help · console · read-only · source
…
  llm-bridge status                       after the provider exits, "Needs you" shows the approve co
mmand
q close | 1/68 4088B 0 matches | / find m mode w wrap s export
```

The golden screen `overlay-help.80x24.ascii` also breaks a word: `approve it only after reading its evid` / `ence.` The page body says `Esc closes this page` while the footer says `q close`. On the home screen, `?` opens the quickstart, whose step 1 is "Run llm-bridge, then press N", although the user is already there.

**Why it hurts users**: Byte counts, "source/rendered" mode and "export" belong to the document viewer, not to help. Mid-word wrapping makes help look broken.

**Fix**: Open help in rendered mode with word wrap on, and use a footer of `↑↓ scroll · / find · Esc close`. Make `?` context-aware on the home screen: "This is the home screen. N new mission · Enter open · / filter". Link to the quickstart from there instead of showing it.

### CLIUX-028 · medium · Console help · `/help` lists an incomplete and duplicated command set

**Evidence**

```
  /status  /tasks  /proposals    print the current state
  /overview /tasks /providers /activity /health /actions /usage   open a view
Commands that run in a shell, such as llm-bridge task add --file F, are the same
commands without the slash.
```

`/tasks` appears twice with two meanings. `/sessions`, `/agents`, `/finish`, `/cancel`, `/steer`, `/rotation`, `/continue` and `/suspend-until-credits` are not listed, although the Actions view offers several of them. "The same commands without the slash" is not true in general: shell commands need flags such as `--task`, while slash forms take positional arguments (`/reject 1 WHY`).

**Fix**: Generate `/help` from the command registry (`bridge_commands`) so it cannot drift, grouped as in CLIUX-018. State the argument mapping explicitly: `/reject N WHY` = `llm-bridge task reject --task N --reason WHY`.

### CLIUX-029 · medium · Actions view · Descriptions are truncated, and Ctrl-K is a view switch, not a palette

**Screen**: F7 Actions at 80 and 100 columns; Ctrl-K.

**Evidence** (golden, 80x24)

```
> Approve task 3                 /approve 3              Accept the finished ...
  Suspend until credits recover  /suspend-until-credits  Arm a recovery wake,...
  Cancel mission                 /cancel                 Cancel mission and u...
```

At 100 columns: `Cancel mission and unfinished tasks (asks …`, `Leave the provider terminal; work keeps ru…`. Ctrl-K switches the tab to `[7 Actions]` with a filter at the bottom. For a STOPPED mission it lists both `Start — Launch the provider on the first ready task` and `Resume — Continue after a pause or a stop`. For a headless mission it lists `Detach — Leave the provider terminal`, although there is none. The Actions `Detach` means "leave your terminal", but the CLI `detach` means "remove another terminal" (`help keys`).

**Why it hurts users**: The description column is what tells a novice what an action does, and it is the column that gets cut. Duplicated or inapplicable actions make the list longer and less trustworthy. The same word means two different actions.

**Fix**: Drop the command column below 100 columns, or show the selected action's full description on a line below the list. Merge Start and Resume into one context-dependent action. Hide actions that do not apply to the mode. Rename the CLI command to `detach-client` (or `station kick`) so that "Detach" always means "leave".

### CLIUX-030 · medium · Learnability · Help prose is written as a specification

**Evidence**

* `help verification`: "Approval marks work complete only through authoritative transition rules. UI approval asks for target-aware confirmation; scripted CLI semantics are unchanged."
* `help states`: "ACTIVE requires a currently executing owned operation; Last selected is history, not execution."
* `help states`: "This queues intent for a safe boundary; acceptance is not proof it already applied. Read submitting/accepted/deferred/applied/rejected feedback and current status."
* `start --help`: "Start the owned controller using saved mode unless overridden."
* `steer --help`: "Queue typed intent for a task or safe execution boundary."

**Why it hurts users**: "Owned controller", "invocation boundary", "typed intent" and "authoritative transition rules" are internal design terms. A novice reading `start --help` cannot tell that it starts the AI.

**Fix**: Rewrite the help in task language. Examples:

* `start`: "Start working on the mission's tasks with the first provider that has capacity."
* `steer`: "Change what happens at the next safe point: replace or review a task, pause after the current step, or hand over to another provider."

Keep the precise wording in `docs/llm-bridge/operations.md`. Add a short glossary topic (`help terms`: mission, task, station, controller, provider, rotation, verify).

### CLIUX-031 · medium · Home form · The provider list does not show who is signed in, and Start is five Tabs away

**Screen**: home `N` form, 100x30.

**Evidence**

```
  Providers:  [1] claude  [ ] codex  [ ] copilot  [ ] opencode  (kilo, minimax not installed)
←/→ choose · Space toggle · numbers show rotation order
```

`doctor` in the same environment reports `codex: installed, not logged in (run: codex login)` and `opencode: installed, not logged in`. The form shows them as ordinary choices. `copilot` (installed) is offered here but is missing from `doctor`'s provider list. Reaching Start takes five Tabs, which the quickstart has to spell out. The subtitle `Run Codex, Claude Code, Copilot and OpenCode on your tasks` lists providers whether they are installed or not (snag 11 in `first-run.md`, not changed), and at 80 columns it is cut: `… · switch as li`.

**Fix**

* Annotate entries: `[ ] codex (not signed in: codex login)`, dimmed, with a hint when selected.
* Make `doctor` and the form use the same provider list.
* Add a direct key for Start from any field (Ctrl-Enter is unreliable in terminals; `Alt-S` or `F5` are safer) and show it on the Objective hint: `Type the task · Alt-S start · Tab more options`.
* Replace "numbers show rotation order" with `Space adds to the rotation; [1] runs first`.
* Shorten the subtitle to fit 80 columns and list only the installed providers.

### CLIUX-032 · medium · Truncation · Names and paths are cut where they are most needed

**Evidence**

```
status:    Mission  f7ae7f30 "Create a file named hello.txt containin…" · RUNNING (native)
missions:  ▌ 14f56611  ‖ STOPPED  Write tests for th…  /tmp/cl…w-cli/repo2  ■ claude  1 Write tests for …
picker 80 (golden): Release 2.~  ~/p~elease  ~/p~porter
Overview Recent: Task 1 added · Create a file named hello.txt containing the word hello, the
```

`missions` truncates the folder in the middle even when stdout is a pipe. The ASCII truncation marker `~` is easily read as the home directory (`~/p~elease`). The Recent row is cut mid-word with no ellipsis.

**Fix**: In `status`, print the full name; it is a two-column key/value layout with room to wrap. In tables, truncate the folder from the left (`…/llm-tools/repo2`), keeping the last path segment whole. Do not truncate when stdout is not a TTY. Use `...` as the ASCII ellipsis, never `~`. Always mark cuts with an ellipsis.

### CLIUX-033 · medium · Command surface · Duplicate commands with different output

**Evidence**

```
$ llm-bridge mission list
ID        STATUS     UPDATED   NAME                                      FOLDER
$ llm-bridge missions
3 missions · 0 live
    ID        Status       Name                 Folder                 Provider  Task
```

The two commands use different column sets and header case, and only `missions` shows the current task. Neither shows task progress or "needs you", which the home list has (`0/1 done`, `◆ REVIEW`). `console --mission ID` (no description, no `--json`) duplicates `control`.

**Fix**: Keep one list command. Make `mission list` an alias of `missions`, with columns ID, STATE, NEEDS YOU, TASKS (done/total), NAME, FOLDER and UPDATED, and the same header case. Hide `console` as internal, since the station uses it.

### CLIUX-034 · medium · Completion · A completed mission gives no next step

**Screen**: console after the last approval.

**Evidence**

```
LLM BRIDGE · ▌ Create a file named hello.txt … · ✓ COMPLETED
mission completed · 1/1 tasks completed
  Controller  mission completed — controller finished
  Now         idle
  Next        nothing ready
```

**Why it hurts users**: This is the end of the first-mission journey. The user does not know whether to add a task, leave, or clean up the station.

**Fix**: Add a Needs you / Next line: `Done. Ctrl-E add a follow-up task · Ctrl-] d leave (the station is closed by llm-bridge gc)`. Print the same line in CLI `status` for completed missions.

### CLIUX-035 · medium · Verifier setup · One flag per argv item is error-prone

**Evidence** (`help verification`)

```
llm-bridge mission create --file prompt.md --verifier-arg python     --verifier-arg=-m --verifier-arg pytest --verifier-arg=-q
```

**Why it hurts users**: Arguments beginning with `-` need `=`, and forgetting it gives an argparse error about an unknown option. This is the feature that turns approval into automatic verification, and it is the hardest one to type.

**Fix**: Accept `--verifier 'python -m pytest -q'`, split with `shlex.split` and shown back in the receipt as an argv list, and `-- python -m pytest -q` at the end of the command. Keep `--verifier-arg` for exact argv.

### CLIUX-036 · low · `doctor --mission` gives the generic next step

**Evidence**

```
$ llm-bridge doctor --mission 14f5
Mission   14f56611 "Write tests for the parser" · STOPPED · …
Next      Ready. In your project folder run llm-bridge, press N, type the task, then choose Start.
```

The mission had a task waiting for review.

**Fix**: With `--mission`, make `Next` reuse the mission's `Needs you` hint (`task 1 awaits approval · llm-bridge task approve --mission 14f5 --task 1`).

### CLIUX-037 · low · Glyphs · ASCII glyphs collide, and three states share one symbol

**Evidence** (golden ASCII screens and `theme`)

* `#` marks both a provider swatch (`# codex`) and VERIFYING/REVIEW (`# VERIFYING`).
* `v` marks COMPLETED (`v COMPLETED`) and the `conserve` guidance (`v conserve`).
* `✕`/`x` marks FAILED, BLOCKED and CANCELLED. `theme` describes `state_failed` as "failed or blocked", so a user's deliberate cancel looks like a failure.

**Fix**: Use distinct ASCII symbols: `?` for review, `*` for running, `+` for done, `!` for failed, `-` for cancelled, `=` for paused, and `@` or `>` for the provider swatch. Give CANCELLED the muted paused-family style rather than the failure colour.

### CLIUX-038 · low · Accessibility · Some identity cues are colour-only

**Evidence**: The mission identity bar `▌` before each mission name and the provider swatch `■` carry meaning only through colour. `mission colour` takes numbers whose meaning is a colour name (`1 teal, 2 azure, … 8 slate`). States always pair a glyph with a word, which is good.

**Fix**: Where the identity colour is the only thing that distinguishes two missions with the same name (as in CLIUX-010), also show the short ID next to the bar. Leave the provider swatch decorative, since the provider name is always printed.

### CLIUX-039 · low · Theme · `theme` does not say how to change the theme, and flag names collide

**Evidence**: `llm-bridge theme` prints the role table and `Theme dark (auto, following the background (COLORFGBG=15;0))` but not how to set a theme. Its flags are `--theme`, `--colors {8,16,256}` and `--color {auto,always,never}`. The product mixes `colour` (`mission colour`, help prose) and `color` (`--color`, `--colors`, the `color` alias).

**Fix**: End the output with `Set it with LLM_BRIDGE_THEME=light or [bridge.theme] name = "light" in ~/.config/llm-tools/config.toml`. Rename `--colors` to `--palette-size`. Choose one spelling for user-facing words and keep the other as a hidden alias.

### CLIUX-040 · low · `steer` overlaps other commands and uses unclear terms

**Evidence**

```
--kind {replace,review,verify,pause-after-iteration,stop-after-task,handoff}
--route NAME   station to hand over to (required for handoff)
$ llm-bridge steer --mission 14f5 --kind review
llm-bridge: review steering requires --task
```

`replace` overlaps `task amend`, `verify` overlaps `task verify`, and `pause-after-iteration` overlaps `pause`. "Iteration" is not defined anywhere a user would look. `--route` is described as a "station".

**Fix**: Keep `steer` for handoff and review only, and route the other kinds to their commands. Name the flag `--to PROVIDER`. Make the error show the missing piece with an example: `--kind review needs --task N and --prompt TEXT, e.g. llm-bridge steer --kind review --task 1 --prompt "check error paths"`.

### CLIUX-041 · low · Distributed · Placement and URL schemes are easy to get wrong

**Evidence** (`help distributed`, `worker --help`)

```
llm-bridge worker enroll --coordinator wss://HOST:8443 …
llm-bridge remote login --coordinator https://HOST:8443 …
llm-bridge worker list                 workers, connection state and last seen   (run on the client, not a worker)
```

`remote` and `worker` have no description line or example. `--target` must come before the command, which is documented only in `help distributed`.

**Fix**: Accept either scheme for the same coordinator and normalise it. Move `worker list` to `remote workers`. Accept `--target` after the command too, or reject it there with `put --target before the command: llm-bridge --target build-1 mission list`.

### CLIUX-042 · low · `rotation` has three mutually exclusive setters

**Evidence**

```
--set LIST        … provider names alone replace the saved rotation, any route name selects route mode
--routes LIST     comma-separated route IDs from the frozen tool config (or provider names)
--providers LIST  comma-separated providers to rotate through; clears saved routes
```

**Fix**: Keep `--set` and infer the mode, as its help already describes. Hide the other two flags. Replace "frozen tool config" with "the config saved with the mission".

### CLIUX-043 · low · Mission naming · The default name is the whole first line

**Evidence**: `--name NAME … (default: first line of the prompt)` gives names like `Create a file named hello.txt containing the word hello, then stop.`, which every table truncates. The create receipt does not show the folder, so a user who ran it in the wrong directory does not notice.

**Fix**: Default to the first line cut at a word boundary under 40 characters. Add the folder to the receipt: `Created mission 14f56611 "Write tests for the parser" in ~/src/parser · task 1 · …`.

### CLIUX-044 · low · `events` uses global IDs and an opaque cursor

**Evidence**: The first event of a new mission is `42`. `--after EVENT … only events with an ID above this (default: 0)`.

**Fix**: Add `--since 10m` and `--follow`. Mention in the help that IDs are global.

### CLIUX-045 · low · `view --file PATH` is a required option

**Evidence**: `usage: llm-bridge view [-h] --file PATH [--follow]`.

**Fix**: Accept a positional path (`llm-bridge view notes.md`) and keep `--file` as an alias.

### CLIUX-046 · low · The same fact is worded differently across surfaces

**Evidence**

* `Next none` in the CLI versus `Next nothing ready` in the console.
* CLI `Needs you`: `task 1 awaits approval · llm-bridge task verify --task 1 --approve (or reject: …)`. Console: `task 1 awaits your approval — type /approve 1 (or F7 Actions)`. The second Needs you item reads `type proposals` without a slash (golden overview).
* `/status` inside the console prints shell commands (`Controller stopped — run: llm-bridge start --mission 14f56611`), while the console's own Overview says `type start or resume`.
* The console response pager uses `|` separators (`Arrows/PgUp/PgDn scroll | Esc/Enter close`) where every other UTF-8 surface uses `·`.

**Fix**: Route all of these through one formatter that takes the surface (CLI or console) as a parameter. Inside the console, render slash forms.

### CLIUX-047 · low · Health shows a running operation after the provider exited

**Evidence**: F6 Health shows `Execution  iteration 1 · operation running 0m · no verified progress yet` while F5 Sessions shows the same operation as `EXITED` and the task is `VERIFYING`.

**Fix**: Read the operation state from the same row Sessions uses, giving `last operation exited 0 · 1m ago · awaiting review`.

### CLIUX-048 · low · Internal flags are visible in help

**Evidence**: `missions --from ID   mission whose station the picker was opened from (used by Ctrl-] then M)`. `attach --client TTY   select a listed writable source client when switching from the Bridge console`.

**Fix**: Suppress `--from` with `argparse.SUPPRESS`. Reword `--client` as `terminal to use when several are attached (for example /dev/pts/3)`.

### CLIUX-049 · low · The mission ID prefix minimum is stricter than it needs to be

**Evidence**: `mission ID prefix '14f' is too short (use at least 4 characters)`, although `14f` matched one mission.

**Fix**: Accept any unique prefix of three or more characters, as `git` does for unique short hashes, and keep the ambiguity listing for non-unique ones. This is low priority, because the error message is already clear.

### CLIUX-050 · low · Composer · The task editor opens blank, with no guidance

**Evidence**: Ctrl-E opens `Add task prompt · full source` with an empty body and the footer `F2/Ctrl-S Save · Esc Recover · Ctrl-D Discard · 0/1048576 bytes · 1 lines`. The golden composer shows that acceptance criteria are written as Markdown (`Acceptance:` / `- tests pass`), but nothing tells a new user this.

**Fix**: Show dimmed placeholder text in an empty composer: `First line = title. Describe the change. Optional: "Acceptance:" followed by "- criterion" lines.` Remove it on the first keystroke.

---

## Things done well (keep these)

* **`doctor` with a `Next` line.** It is offline, fast, and says exactly what to do (`not logged in (run: codex login)`). The JSON form carries `next_steps`.
* **Opening Bridge never starts a provider.** The Start row states the cost (`Starts claude now · uses provider capacity`) and the Claude trust-prompt warning. Every help page states whether the command can use paid capacity; CLIUX-017 asks only to make that line accurate and specific.
* **The approval confirmation dialog.** It names the mission and task, warns when there is no evidence, and states whether the workspace changed. Build on it (CLIUX-003) rather than replacing it.
* **The `Needs you` row** in CLI `status` and in the Overview is the right primary signal, and it carries a copy-pasteable command.
* **Folder-inferred missions.** When inference is ambiguous, the error lists the candidates with status and `(live)` markers. Mission-not-found and prefix-too-short errors name the fix (`run llm-bridge mission list`).
* **Explained conflicts.** `--headless and --native cannot be combined: …` explains both modes, and `rotation --set` lists the available entries.
* **The console's unknown-command handling** (`did you mean approve?`). Bring it to the CLI (CLIUX-007).
* **Narrow terminals degrade gracefully.** At 60x18 and 40x12 the tab bar collapses with `>`, rows wrap, and nothing overlaps.
* **ASCII and UTF-8 parity.** Every screen has an ASCII rendering. States always pair a glyph with a word, so the console is usable with no colour. The golden-screen suite pins this.
* **Non-TTY fallbacks**, where they exist: `missions --pick` explains itself, `view` prints the file, and bare `llm-bridge` prints help. Extend them to `usage` and `control` (CLIUX-004).
* **Detach semantics.** `Ctrl-] d` is described clearly as safe over SSH, `Ctrl-] Ctrl-]` sends a literal prefix, and detaching never stops work.
* **The quickstart matches the real flow.** Every step of `help quickstart` and the README first-mission section was reproduced as written.
* **Rejection requires a reason** (`--reason` is mandatory, and `/reject N WHY` asks for one), which keeps the history useful.
* **Destructive console actions confirm** (`/approve`, `/cancel`, `/reject`), and the confirmation names the target mission ID.

---

## Second reviewer

An independent reviewer, briefed as a principal UX designer for developer tools and given the pre-review's headings only to avoid duplicates, reviewed the same scope on 2026-11-06 at commit 8ecd9ae.

Reviewer: independent second review, 2026-11-06, checkout `feat/distributed-release-readiness` (`8ecd9ae`), `llm-bridge 1.4.0`.

Method: every `--help` page and help topic was captured with a throwaway `HOME`, state and XDG directories. Headless missions were driven from the CLI with a fake provider (`--command-template "python3 ok.py"`), once with no capacity data and once with an injected `LLM_SCHEDULER_USAGE_JSON` snapshot. The home screen, new-mission form, station strip and console were driven at 80x24 in a private tmux server started with `env -i`, so no inherited Bridge variables reached it. Golden screens in `tests/golden/screens/` were read at 80x24 and 140x40, UTF-8 and ASCII. Theme roles were read with `llm-bridge theme --theme light|dark --json`, and contrast was computed against white and black backgrounds. No real mission, real state directory or coordinator was touched. The only real provider CLI that the form selected (Copilot) never ran, because Bridge put it into a capacity wait (see CLIUX-055).

### Summary

The pre-review covered the vocabulary, help and JSON problems. This review found a second class of problem: **status that is wrong while work is not happening**. Bridge often shows `RUNNING`, or a countdown, or a hint, that does not match what the controller is doing.

* A task shows `RUNNING · operation RUNNING` for up to 15 minutes while the scheduler is still waiting for capacity data, and the countdown restarts every minute (CLIUX-051).
* After `stop --force`, `start` runs a controller that never resumes the task. Status blames capacity, while the task is actually blocked (CLIUX-053).
* The home form says `Starts copilot now` for a provider whose quota resets in 24 days, and the mission then waits for 24 days under a `● RUNNING` header (CLIUX-055).
* A receipt prints `start it with: llm-bridge start` without `--mission`. Run from another folder, that command starts a different mission (CLIUX-052).

Ordering is also inconsistent:

* `task list` does not show the order in which tasks run.
* `P0` is the *lowest* priority.
* `events` shows the oldest events first, so the command that `Needs you` and `help recovery` recommend shows mission creation instead of the failure (CLIUX-056 to CLIUX-058).

The rest are smaller problems in wording, layout and scripting.

### Count by severity

| Severity | Count |
| --- | --- |
| Critical | 0 |
| High | 4 |
| Medium | 12 |
| Low | 14 |
| Total | 30 |

---

### Second reviewer findings

#### CLIUX-051 · high · System status · A task shows RUNNING for up to 15 minutes while no provider runs, and the countdown restarts

**Command/Screen**: `llm-bridge status` for a headless mission whose provider has no capacity data (for example, a provider that is not signed in, or one whose usage endpoint is unreachable).

**Evidence**

```
$ llm-bridge mission create --prompt "OK job" --headless --providers claude --command-template "python3 ok.py"
$ llm-bridge start
$ llm-bridge status            # at +6 s, +21 s, +40 s and about 3 min
Current     task 1 "OK job" · RUNNING · operation RUNNING
Tasks       1 total: 1 RUNNING
Capacity
  claude        no capacity data · unavailable
Waiting     next provider capacity in 53s      (then 34s, 22s, and 41s again)
$ ls
(empty: out.txt was never written)
```

The scheduler's run log shows the real state: `usage decision: unavailable`. `scheduler.py:60` gives the default: `--max-unavailable-wait SECONDS … (default: 900)`. The provider was not launched until that 900 s wait ran out. `llm-bridge stop` during this wait printed `STOPPING` and kept `operation RUNNING`, because it waits for a provider process that does not exist yet.

**Why it hurts users**: The status says that paid work is in progress when no provider has started. The `Waiting … in 53s` countdown reaches about 20 s and then jumps back up, so it never predicts anything. A first-run user whose provider is not signed in sees `RUNNING` for 15 minutes and has no hint that sign-in is the problem.

**Fix**: Do not mark the task or the operation `RUNNING` until the provider process has started. Use a distinct state, `○ WAITING for capacity data (claude: no usage data · launches anyway in 14m)`, and show the deadline as an absolute time that does not restart. When the reason is `not-authenticated` or `missing-cli`, put that reason and the sign-in command in `Needs you`. Make plain `stop` complete at once when no provider process exists.

#### CLIUX-052 · high · Error prevention · `Mission reopened · start it with: llm-bridge start` can start a different mission

**Command/Screen**: `task add --mission ID` for a completed mission, run from another folder.

**Evidence**

```
(cwd: …/env/repo, which has its own unfinished missions; mission 4141ac50 lives in …/repo3 and was COMPLETED)
$ llm-bridge task add --mission 4141 --pri 3 --prompt x
Added task 2 "x" · QUEUED · priority 3
Mission reopened · start it with: llm-bridge start
```

`bridge_format.py:268` always appends the hint without `--mission`:
`text += "\nMission reopened · start it with: llm-bridge start"`.
In a folder that has exactly one unfinished mission (`…/folderA`, mission `4cda1a45`), `llm-bridge status` resolves to that mission. The printed `llm-bridge start` would therefore start paid work on `4cda1a45`, not on the mission that was reopened.

**Why it hurts users**: Users copy and paste the receipt. The command it suggests can spend capacity on the wrong mission without any warning. This differs from CLIUX-010, where the suggested command fails safely on ambiguity.

**Fix**: Always print the resolved mission in receipts: `Mission 4141ac50 reopened · start it with: llm-bridge start --mission 4141ac50`. Add a test that runs `task add --mission X` from a folder that belongs to mission Y and checks that every command in the receipt names X.

#### CLIUX-053 · high · Recovery · After `stop --force`, `start` runs a controller that never resumes the task, and status blames capacity

**Command/Screen**: `stop --force` during a run, then `start`, then `status`.

**Evidence**

```
$ llm-bridge stop --force
llm-bridge: no verifiable process identity; mission remains STOPPING        (exit 2)
$ llm-bridge status
Mission     43f80dda "OK job" · STOPPED (headless)
Current     task 1 "OK job" · BLOCKED
Needs you   task 1 is blocked, its provider run ended without a result · check llm-bridge events, then llm-bridge task retry --task 1
$ llm-bridge start
mission 43f80dda running without a terminal station · inspect with: …          (exit 0)
$ llm-bridge status
Mission     43f80dda "OK job" · RUNNING (headless)
Current     task 1 "OK job" · BLOCKED
Capacity
  claude        no capacity data · unavailable
Waiting     for a usable provider · claude resets in <1m · change providers with: llm-bridge rotation --mission 43f80dda --set LIST
```

`events` shows the cause: `operation-uncertain  task 1 reason=operation outcome was not committed bef…`.

**Why it hurts users**: The user stopped the work. Bridge reports that "its provider run ended without a result", which suggests a provider fault. `start` then reports success and shows a mission `RUNNING` that will never run the task. The `Waiting` line gives a third explanation: it claims a reset in under a minute for a provider that has `no capacity data`. That reset time was invented. Each of the three surfaces gives a different, wrong reason.

**Fix**: Record a forced stop as `task 1 interrupted by stop --force`. On the next `start`, re-queue an interrupted task automatically, or refuse with `task 1 was interrupted by stop --force · llm-bridge task retry --task 1, then start`. Show the `Waiting` line only when capacity is the actual blocker, and never show a reset time for a provider that has no capacity data.

#### CLIUX-054 · medium · Error messages · `stop --force` reports an internal failure that did not happen

**Command/Screen**: `llm-bridge stop --force` on a headless mission that is waiting for capacity (CLIUX-051).

**Evidence**

```
$ llm-bridge stop --force
llm-bridge: no verifiable process identity; mission remains STOPPING     (exit 2)
$ llm-bridge status
Mission     43f80dda "OK job" · STOPPED (headless)
```

A second `stop --force` printed `Stop forced · mission 43f80dda is STOPPED`.

**Why it hurts users**: "Verifiable process identity" is an internal term. The message says the mission is still `STOPPING`, but it is `STOPPED` a moment later. A failure exit for a stop that succeeded makes scripts retry, and makes users think something is stuck.

**Fix**: When no provider process exists, report `Stopped · no provider process was running` with exit 0. Report a real failure only when a process is still alive: `could not confirm that provider process 12345 belongs to this mission; it was left running. Check with: ps -p 12345`.

#### CLIUX-055 · high · Error prevention · The home form starts a provider whose quota is empty for 24 days, and the mission then shows RUNNING

**Command/Screen**: Home → `N` → Start, at 80x24, in an environment where Copilot is the only installed provider and its monthly quota is at 0%.

**Evidence**

```
Start row hint:      Starts copilot now · uses provider capacity
After Enter:         Waiting for capacity: copilot monthly quota resets in 24d 5h. Ctrl-] then B for
                     options.
Controls header:     LLM BRIDGE · ▌ Write hello.txt · ● RUNNING
                     waiting for capacity · copilot monthly 0% · resumes in 24d 5h
F3 Providers:        > ■ copilot  default model  rate-limited, resets in 24d 5h
$ llm-usage --json   copilot: {"source": "copilot api", "available": true, "monthly": {"used": 100.0, "remaining": 0.0}}
$ llm-bridge doctor  (does not list copilot at all; Next: "Install and sign in to a provider CLI (Claude Code or Codex)")
```

**Why it hurts users**: The Start row says the provider starts now. Instead, the user gets a station that waits for 24 days under a `● RUNNING` header. The form already has the usage data that shows this. The form, `doctor` and the console disagree about whether Copilot is a usable provider.

**Fix**: In the form, annotate an exhausted provider: `[ ] copilot (quota empty · resets in 24d)`. Do not select it by default. Change the Start hint to `copilot has no quota until Dec 1 · Save mission only, or choose another provider`. In the console header, show `○ WAITING` (not `● RUNNING`) while no operation is executing. Make `doctor` check the same provider list as the form (see CLIUX-031).

#### CLIUX-056 · medium · Information architecture · `task list` does not show the order in which tasks will run

**Command/Screen**: `llm-bridge task list` compared with `status` `Next` and the console Tasks view.

**Evidence**

```
$ llm-bridge task priority --task 3 --value -5
Priority set for task 3 · QUEUED
$ llm-bridge task reorder --task 1 --value 99
Reordered task 1 · QUEUED
$ llm-bridge task list
TASK  STATUS  PRIORITY  AFTER  TITLE
2     QUEUED  0                Second task
3     QUEUED  -5        2      Third
1     QUEUED  0                Write tests for the parser
```

The scheduler runs `ORDER BY priority DESC,ordering,id` (`bridge_store.py:899`), which gives the order 2, 1, 3. `task list` sorts with `ORDER BY ordering,id` (`bridge_store.py:859`), which gives 2, 3, 1. The console Tasks view sorts by priority (golden `console-tasks.80x24.utf8`: P2, P1, P1, P0 …). The ordering value that `reorder` changes is not shown in any table, and neither receipt shows the new value.

**Why it hurts users**: Users reorder work to control what runs next. The CLI table shows a different order from the one the controller will use, and from the order in the console.

**Fix**: Sort `task list` the same way the scheduler sorts (with finished tasks last, or in a separate group), and add an `ORDER` column or a `#` "runs as" column. Make the receipts show the result: `Priority of task 3 set to -5 · runs after tasks 2 and 1`.

#### CLIUX-057 · medium · Vocabulary · `P0` is the lowest priority, and priority and order sort in opposite directions

**Command/Screen**: Console Tasks view; `task priority --help`; `task reorder --help`.

**Evidence**

```
golden console-tasks.80x24.utf8, rows 03-08:
  2   ✕ FAILED     P2           Fix the flaky login test
  4   ● RUNNING    P1           Update the CI matrix
  1   ✓ COMPLETED  P0           Audit dependency licences
task priority --value N   new priority; higher runs first
task reorder  --value N   new ordering value; lower runs first
```

Negative values are accepted, so the view can show `P-5`.

**Why it hurts users**: In issue trackers and incident tooling, `P0` means the most urgent item. In Bridge, `P0` is the default, lowest bucket, and `P2` runs before it. Two numeric fields that both affect run order, sorted in opposite directions, will be set the wrong way round.

**Fix**: Do not display `P<n>`. Show `prio 2`, or show nothing for the default and `↑2` for raised priority. Better still, replace the two fields with one operator action: `task move --task 3 --before 1` or `--first`, and keep `priority` and `order` as internal sort keys.

#### CLIUX-058 · medium · Diagnostics · `events` shows the oldest events first, so the recommended `--limit 20` never reaches the failure

**Command/Screen**: `llm-bridge events --limit N`, which `help recovery` and the `Needs you` hints recommend.

**Evidence**

```
$ llm-bridge events --limit 5
ID  TIME            KIND             DETAIL
1   11-06 18:51:56  mission-created  cwd=/tmp/… name=Write tests for the parser
2   11-06 18:51:56  task-added       task 1 priority=0 title=Write tests for the parser
3   11-06 18:51:56  command-queued   …
4   11-06 18:51:56  task-added       task 2 …
5   11-06 18:51:56  command-applied  …
(no "more events" line)
```

The query is `… WHERE e.mission_id=? AND e.id>? ORDER BY e.id` with `--after` defaulting to 0 (`bridge_store.py:1559`). `help recovery` says: `llm-bridge events --mission ID --limit 20 --json`. `Needs you` says: `check llm-bridge events, then llm-bridge task retry`. A controller writes a heartbeat every 5 s, so after a few minutes the default `--limit 100` shows only the start of the mission.

**Why it hurts users**: The command that Bridge recommends for diagnosis shows the creation history and hides the failure. Nothing on the screen says that more events exist.

**Fix**: Default to the newest N events, printed oldest to newest (as `git log -n` and `journalctl -n` do). Print `showing the last 20 of 140 · --after 120 for older` when the output is truncated. Make `Needs you` link to the relevant range: `llm-bridge events --task 1`.

#### CLIUX-059 · medium · Concepts · `/detach` in the console and `llm-bridge detach` in a shell are described as opposites

**Command/Screen**: F7 Actions, `help console`, `help keys`, `detach --help`.

**Evidence**

```
Actions (bridge_actions.py:57):  Detach   /detach   Leave the provider terminal; work keeps running
help console:                    /attach  /detach    open the provider terminal; Ctrl-] P returns
help keys:                       llm-bridge detach --mission ID is for another shell … usually a different terminal
quickstart:                      The command llm-bridge detach is different: it removes a station client from outside …
                                 Do not use it to leave your own session.
help console:                    Commands that run in a shell … are the same commands without the slash.
```

In code, both forms call `TmuxTerminalBackend.detach_client`, which selects "the station's one connected writable client" (`terminal.py:1094`, `1716`, `1729`).

**Why it hurts users**: The help says that slash commands and shell commands are the same. It then says that the shell command must not be used to leave your own session, while the Actions view offers the slash command for exactly that. `help console` describes `/detach` as opening the provider terminal. With a local terminal and an SSH terminal both attached, a user cannot predict which one `/detach` removes.

**Fix**: Give the two operations different names. Use `/leave` (or only `Ctrl-] d`) for "detach my terminal", and `detach --client` for "remove another terminal". Describe each one in a single sentence, and use the same sentence in Actions, `help console` and `--help`.

#### CLIUX-060 · medium · Navigation · The Overview tells you to press Ctrl-] B while you are already in the Ctrl-] B controls

**Command/Screen**: Console Overview, `Needs you` row, during a capacity wait.

**Evidence**

```
  Needs you   Waiting for capacity: copilot monthly quota resets in 24d 5h.
              Ctrl-] then B for options.
▶ Bridge: Ctrl-] then B │ … │ Provider: Ctrl-] then P
```

The sentence comes from `bridge_monitor.waiting_message`, whose docstring says it is "The sentence shown in an empty provider pane". The Overview reuses it unchanged.

**Why it hurts users**: The one row meant to say what to do sends the user to the screen they are already on. The real options (`/rotation`, `/stop`, `/suspend-until-credits`) are not named.

**Fix**: Give the console its own wording: `Waiting for capacity: copilot resets in 24d 5h · /rotation to add a provider · /stop to stop waiting`. Keep the `Ctrl-] then B` sentence only for the provider pane.

#### CLIUX-061 · medium · Status consistency · A READY task is described as "nothing ready" and "no task waiting"

**Command/Screen**: Console Overview after a stop, 80x24.

**Evidence**

```
LLM BRIDGE · ▌ Write hello.txt · ‖ STOPPED
stopped · no task waiting · Ctrl-E adds one
  Controller  stopped — type start or resume
  Now         task 1  ○ READY  Write hello.txt
  Next        nothing ready
  Tasks       1 ready
```

**Why it hurts users**: Three lines on one screen contradict each other. The header tells the user to add a task, although a task is ready and only needs `start`. The label `Now` suggests that task 1 is executing.

**Fix**: When the mission is stopped, rename `Now` to `Up next` (or show `Now  nothing running`), and put the ready task under `Next`. Change the header line to `stopped · task 1 ready · type start`. Generate all three lines from the same task query.

#### CLIUX-062 · medium · Vocabulary · `RUNNING stale` and `PAUSED stale` are unexplained, and RUNNING is shown for a mission with no controller

**Command/Screen**: Home list and mission picker.

**Evidence**

```
golden mission-picker.80x24.utf8 row 05:  › ▌ 000000c3  ● RUNNING   stale  Investigat…  ~/w…ampler  ■ codex  1 Reprodu…
                                  row 22:  ~/work/sampler · codex → claude · controller not running
golden home-list.80x24.utf8 row 07:  > ‖ PAUSED    stale  000000b2  ▌ Port the exporter …
                               row 22:  controller not running · Enter opens controls, then type resume
```

`stale` is set when a mission that should have a controller has none (`bridge_launcher.py:561`). Neither help nor the screen defines the word.

**Why it hurts users**: This is the state that every mission shows after a reboot or a crash. A green `● RUNNING` glyph next to a yellow `stale` reads as "running, data is old", not as "stopped unexpectedly, resume it".

**Fix**: Replace the mission state with `✕ INTERRUPTED` (or `‖ STOPPED (interrupted)`) when the controller is gone, and drop the separate `stale` column. Use the same word in `status`, the picker and the home list. Put the action in the detail line: `interrupted at 03:12 · Enter, then resume`.

#### CLIUX-063 · medium · Error recovery · Commands in a finished mission's folder fail with one message that does not fit most of them

**Command/Screen**: `task add`, `attach`, `finish`, `detach` and `station` in the folder of a completed mission, without `--mission`.

**Evidence**

```
$ llm-bridge task add --prompt "follow up"        (in …/repo3, mission 4141ac50 COMPLETED)
llm-bridge: mission 4141ac50 in folder …/repo3 is finished (completed); add follow-up work with: llm-bridge task add --mission 4141ac50 --prompt "..." or create a new mission with: llm-bridge mission create --prompt "..."
$ llm-bridge attach      → the same "add follow-up work …" message
$ llm-bridge detach      → the same message
$ llm-bridge station claude → the same message
```

In the same folder, `status`, `task list`, `task show` and `events` resolve the finished mission without complaint.

**Why it hurts users**: For `task add`, Bridge has identified the mission and asks the user to type the same command again with the ID it already found. For `attach` and `detach`, a message about adding follow-up work does not answer the question the user asked.

**Fix**: For `task add`, act on the only mission in the folder and print the reopen receipt (with `--mission`, see CLIUX-052). Ask only when several finished missions match. For station commands, say `mission 4141ac50 is completed and has no station · llm-bridge status for its result`.

#### CLIUX-064 · medium · Information hierarchy · Synthetic exit codes (75) are shown without their meaning

**Command/Screen**: Activity, Overview Recent, Providers (golden screens).

**Evidence**

```
console-activity.80x24.utf8 row 14:   03:12:50  Provider exited (75) on task 6
console-providers.80x24.utf8 row 14:      recent: task 6 · exited · exit 75 · 20m ago
console-overview.140x40.utf8 row 23:  03:12:50  Provider exited (75) on task 6
```

AGENTS.md defines status 75 as the scheduler's autonomy abort: an idle timeout, a blocking prompt, a hung tool call, or a provider that is silently rate-limited. Status 76 means that the operator declined the folder-trust prompt.

**Why it hurts users**: Users read `75` as an error code from the provider and search for it in the provider's documentation. These are the runs that most need explaining, because Bridge stopped them, not the provider.

**Fix**: Map the synthetic statuses to words in every surface: `Stopped by Bridge: no output for 10m (idle timeout)` or `Stopped by Bridge: provider was waiting for input`. Show the number only in `--json` and `events`. Do the same for 76 (`you declined the folder-trust prompt`) and 130/143 (`interrupted`).

#### CLIUX-065 · medium · Accessibility · The ASCII fallback never applies for `LANG=C`, and there is no override

**Command/Screen**: Any curses screen over SSH with a non-UTF-8 terminal.

**Evidence**

`bridge_glyphs.ascii_mode` documents itself as the fallback for "``LANG=C`` over SSH". It reads `locale.nl_langinfo(CODESET)`. Python 3.7 and later coerce the C locale to UTF-8 (PEP 538):

```
$ env -i LANG=C python3 -c "import locale;print(locale.nl_langinfo(locale.CODESET))"
UTF-8
$ env -i LANG=C LC_ALL=C python3 -c "…"
ANSI_X3.4-1968
```

At 80x24 with `LANG=C`, the home screen and the console drew `●`, `‖`, `▌` and `·`. No `LLM_BRIDGE_*` variable forces ASCII glyphs.

**Why it hurts users**: Only users who set `LC_ALL=C` get the ASCII screens that the golden suite tests. A terminal that really is not UTF-8 (a serial console, an old PuTTY profile, some CI log viewers) shows mojibake in the state column, which is the column that carries the meaning.

**Fix**: Decide the glyph set from `LC_ALL`/`LC_CTYPE`/`LANG` as the user set them, before coercion, or from `sys.flags.utf8_mode` together with the original environment. Add `LLM_BRIDGE_GLYPHS=ascii|utf8` as an explicit override, and document it in `help keys` and `theme`.

#### CLIUX-066 · medium · Exit codes · `doctor` exits 0 when no provider can run, and its JSON has no overall verdict

**Command/Screen**: `llm-bridge doctor`, `llm-bridge doctor --json`.

**Evidence**

```
$ llm-bridge doctor
Providers        claude: not on PATH · install it, or remove it from the rotation with: llm-bridge rotation --set LIST
                 codex: not on PATH · …
                 opencode: not on PATH · …
Next             Install and sign in to a provider CLI (Claude Code or Codex), then run llm-bridge doctor again.
exit 0
$ llm-bridge doctor --json | jq keys
["database","mission","next_steps","providers","schema_version","sqlite_integrity","state_dir","tmux_available"]
```

On a machine with no mission, `doctor` advises `remove it from the rotation with: llm-bridge rotation --set LIST`, which needs a mission. `Next` names only Claude Code and Codex, although the home subtitle advertises Codex, Claude Code, Copilot, Kilo and OpenCode.

**Why it hurts users**: Install scripts and CI cannot gate on `doctor`. `brew doctor` and `flutter doctor` both exit non-zero when a blocking problem is found. The per-provider advice suggests a command that cannot work yet.

**Fix**: Exit 1 when `Next` is not `Ready`, and add `"ready": false` and `"blocking": [...]` to the JSON. Without `--mission`, drop the rotation advice and say `install one of: claude, codex, copilot, opencode, kilo`. Generate that list from the provider registry.

#### CLIUX-067 · low · Home form · The Objective hint says "Tab moves on to Start", but Tab moves to Providers

**Command/Screen**: Home → `N`, Objective field, 80x24. See also CLIUX-031.

**Evidence**

```
Type the task · Tab moves on to Start · Ctrl-E full editor · paste supported
(Tab)
> Providers: ▸[1] copilot  (codex, claude, kilo, opencode, minimax not installed
```

`help quickstart`, step 3, says: "Press Tab five times to reach 'Start and open provider'".

**Why it hurts users**: This is the first hint a new user reads, and the first key press proves it wrong. The help and the screen give different instructions.

**Fix**: Change the hint to `Tab next field · Alt-S start now`, together with the direct Start key that CLIUX-031 proposes. Until that key exists, say `Tab ×5 reaches Start`.

#### CLIUX-068 · low · Home screen · The empty state names providers that are not installed, and wraps in the middle of a phrase

**Command/Screen**: Home screen with no missions, 80x24, with only Copilot installed.

**Evidence**

```
Run Copilot on your tasks
…
A mission is a goal plus ordered tasks run by Codex/Claude in this folder · N
to create one
```

**Why it hurts users**: The subtitle is built from the installed providers. The explanation underneath is fixed text, so it contradicts the subtitle. The line breaks between `N` and `to create one`, which splits the key from its action.

**Fix**: Build the sentence from the same provider list, or make it neutral ("run by your AI coding CLI"). Wrap at word boundaries and keep `N to create one` together.

#### CLIUX-069 · low · Glyphs · In ASCII mode, the header shows `| |`, because the identity bar and the separator are the same character

**Command/Screen**: Every console view in ASCII mode.

**Evidence**

```
golden console-overview.80x24.ascii row 00:  LLM BRIDGE | | Release 2.0 hardening | * RUNNING
golden console-overview.80x24.utf8  row 00:  LLM BRIDGE · ▌ Release 2.0 hardening · ● RUNNING
```

**Why it hurts users**: `| |` looks like an empty field or a rendering fault on the first row of every screen. This is separate from the collisions listed in CLIUX-037.

**Fix**: In ASCII mode, drop the identity bar from the header (it carries colour only), or map `·` to ` - ` or `  `, so that the bar stays distinct from the separator.

#### CLIUX-070 · low · CLI layout · `status` prints an empty `Capacity` heading

**Command/Screen**: `llm-bridge status` for a STOPPED or COMPLETED mission.

**Evidence**

```
Tasks       1 total: 1 COMPLETED
Capacity
$
```

**Why it hurts users**: A heading with nothing under it looks like a failed read.

**Fix**: Omit the section when there is no snapshot, or print `Capacity    not observed while stopped`.

#### CLIUX-071 · low · Error prevention · Long options accept prefixes, so `stop --f` means `--force`

**Command/Screen**: Any command (argparse `allow_abbrev` is left at its default).

**Evidence**

```
$ llm-bridge stop --f --mission 4141
Stop forced · mission 4141ac50 is STOPPED
$ llm-bridge mission create --prompt q --auto --json      → accepted as --auto-continue
$ llm-bridge status --mis 4141                            → accepted as --mission
```

**Why it hurts users**: A two-character prefix runs the destructive form of `stop`. Scripts that rely on prefixes break, or change meaning, as soon as a new option with the same prefix is added.

**Fix**: Set `allow_abbrev=False` on every parser. Add the real short aliases instead (CLIUX-080).

#### CLIUX-072 · low · Consistency · Three pagination models across list commands

**Command/Screen**: `events --help`, `proposal list --help`, `task list --help`.

**Evidence**

```
events         --after EVENT   only events with an ID above this (global across missions)
proposal list  --after N       only proposals with a task number above this
task list      --offset N      skip the first N tasks of the list   (and --all, --limit)
```

**Why it hurts users**: The same word, `--after`, means a global event ID in one command and a per-mission task number in another. Offset-based paging is a third model. Scripts written for one command fail silently on another. This extends CLIUX-044.

**Fix**: Use one model: `--limit N` plus `--after CURSOR`, where the cursor is opaque and printed in the "more" line, or use `--offset` everywhere. Name the cursor in the output: `more: --after 120`.

#### CLIUX-073 · low · Time display · Activity and Recent show times without dates

**Command/Screen**: Activity view, Overview Recent; `events`.

**Evidence**

```
Activity:  03:13:50  Waiting for capacity, resumes in 25m …     (bridge_tui._local_clock: "%H:%M:%S")
events:    11-06 18:51:56                                       (bridge_format.py:401: "%m-%d %H:%M:%S")
```

Capacity waits can last days (CLIUX-055 waits 24 days), and suspended missions resume after many hours.

**Why it hurts users**: `03:13:50` from yesterday reads as this morning. `11-06` is ambiguous between month-day and day-month, and has no year.

**Fix**: In views, show `HH:MM:SS` for today, `Mon 03:13` within the last week, and `2026-11-06 03:13` otherwise. In `events`, use ISO 8601 (`2026-11-06 18:51:56`).

#### CLIUX-074 · low · Help · Four different lists of help topics

**Command/Screen**: `help --help`, `help quickstart`, `help nosuch`, `/help` (`help console`).

**Evidence**

```
help --help:      quickstart, keys, remote, prompts, markdown, providers, states, verification, recovery
help quickstart:  help keys, remote, distributed, prompts, markdown, providers, states, verification, recovery
help nosuch:      quickstart, keys, console, remote, distributed, prompts, markdown, providers, states, verification, recovery
help console:     /help [topic] … topics: keys, prompts, providers, states
```

**Why it hurts users**: `console` and `distributed` exist but appear in only some lists. Users cannot find a topic that a list leaves out.

**Fix**: Generate every list from one registry in `bridge_help`, give each topic a one-line description, and print them as a table in `llm-bridge help`.

#### CLIUX-075 · low · Error messages · `task verify` without flags does not name the flags it needs

**Command/Screen**: `llm-bridge task verify --task 1`.

**Evidence**

```
llm-bridge: completion requires verification evidence or explicit operator approval
```

**Why it hurts users**: The message states a policy, not the fix. "Operator approval" does not tell the user to add `--approve`.

**Fix**: `task 1 needs --approve to complete, or --evidence TEXT to record evidence without completing it · example: llm-bridge task verify --task 1 --approve`.

#### CLIUX-076 · low · Key hints · Footers are cut without an ellipsis, and the key to leave is lost

**Command/Screen**: Home screen after saving, 80x24; Overview, 140x40.

**Evidence**

```
golden home-saved.80x24.utf8 row 23:     ↑↓ select · Enter controls · S start · N new mission · / filter · ? help · Q qu
golden console-overview.140x40.utf8 row 38:  › type a command · /help · Ctrl-K actions
                                    row 39:  o objective · Ctrl-E add task · Tab/F1-9 views · Ctrl-D exit · Ctrl-K actions · ? help
```

In the same golden set, the Enter key is labelled `Enter open` in `home-list` and `Enter controls` in `home-saved`.

**Why it hurts users**: How to quit is the hint a lost user needs most, and it is the one that gets cut. Duplicated hints use space that the 80-column layout needs.

**Fix**: Drop the lowest-priority hints first, and always keep `? help` and the quit key. Never repeat a hint that appears on the prompt line. Use one label for Enter on the home list.

#### CLIUX-077 · low · First run · Bare `llm-bridge` silently prints help when it cannot open the home screen

**Command/Screen**: `llm-bridge` with no arguments, when stdin or stdout is not a TTY, `TERM` is empty or `dumb`, or `LLM_BRIDGE_MANAGED=1`.

**Evidence**

`bridge._main`: `if … sys.stdin.isatty() and sys.stdout.isatty() and TERM not in {"", "dumb"} and not _managed_session(): run_launcher(…)`, otherwise `_build_parser().print_help(); return 0`. Inside a Bridge-managed session, a 24-line argparse page appeared in place of the home screen, with exit 0 and no reason given.

**Why it hurts users**: A user on `ssh host llm-bridge` (no `-t`) or with `TERM=dumb` expects the home screen. They get a help page and cannot tell why.

**Fix**: Print a one-line reason before the help: `llm-bridge: the home screen needs an interactive terminal (stdout is not a TTY); showing help instead`. Exit 2 when the reason is a missing TTY, as `missions --pick` does.

#### CLIUX-078 · low · Error prevention · `--providers ""` and unknown `--routes` create missions that cannot run

**Command/Screen**: `mission create`. This adds new cases to CLIUX-002.

**Evidence**

```
$ llm-bridge mission create --prompt x --providers ""
Created mission adfac9e2 "x" · task 1 · … · start with: llm-bridge start --mission adfac9e2      (exit 0)
$ llm-bridge rotation --mission adfac9e2
No rotation saved for this mission.
$ llm-bridge mission create --prompt "y" --routes nosuch-route
Created mission e6d19655 "y" …                                                                  (exit 0)
```

`rotation --set ""` rejects the same input: `a rotation needs at least one provider or route name`.

**Fix**: Run the `rotation --set` validator for both flags in `mission create` and `start`. An empty list is an error. An unknown route lists the configured route IDs.

#### CLIUX-079 · low · Recovery · `attach` to a stopped mission offers only paid work

**Command/Screen**: `llm-bridge attach` for a native mission whose station has gone.

**Evidence**

```
llm-bridge: No provider station is running for mission a4ba439f · start it with: llm-bridge start --mission a4ba439f
```

**Why it hurts users**: Users often attach only to look at what happened. The only way out the message offers starts a provider and uses capacity.

**Fix**: Offer the free option first: `No station is running · inspect with: llm-bridge control --mission a4ba439f · or start work with: llm-bridge start --mission a4ba439f (uses capacity)`.

#### CLIUX-080 · low · Ergonomics · No short options, and task numbers are required options rather than positional arguments

**Command/Screen**: Every command page (see `mission create --help`, `task verify --help`).

**Evidence**

The only short option in the CLI is `-h`. The most frequent command in the review loop is:

```
llm-bridge task verify --mission 3f9a1c2b --task 1 --approve
```

`gh`, `git` and `kubectl` accept `-R`/`-n`, and positional IDs (`gh pr view 12`). CLIUX-045 covers only `view --file`.

**Why it hurts users**: The commands are long to type and to read, and users who know `gh` or `git` expect `llm-bridge task approve 1`.

**Fix**: Add `-m/--mission`, `-t/--task` and `-j/--json`, and accept the task number as an optional positional argument on every `task` subcommand (`llm-bridge task show 1`). Keep `--task` for compatibility.

## Fix status

| Finding | Severity | Status | Fix and test |
|---|---|---|---|
| CLIUX-001 | critical | fixed | 82a36cf. `start` refuses cancelled and completed missions (same rule as `resume`); it prints "running" only after the controller reports RUNNING; a controller that fails at start-up prints the reason and a fix and exits 2. The controller now rejects unknown rotation names before reporting ready. `tests/test_bridge_cli_start_create_ux.py`::test_start_refuses_a_cancelled_mission, test_launch_reports_a_controller_that_started_nothing and three more; failed before the fix (exit 0, "running"). |
| CLIUX-002 | critical | fixed | 82a36cf. One validator (`bridge_rotation.validate_names`) for `rotation --set`, `mission create`, `start --providers/--routes` and the controller, with did-you-mean; a stderr warning for installed providers that are not signed in. `tests/test_bridge_cli_start_create_ux.py`::test_create_rejects_an_unknown_provider_with_a_suggestion and five more; failed before the fix. |
| CLIUX-003 | high | fixed | 075d38c. New `task output` (pages the latest run log; reports pruned logs with size and sha256) and `task diff` (diff from the start-of-run commit recorded in the checkpoint, plus untracked files). The approval dialog lists changed files and adds `o output · d diff`; the Tasks inspect pane shows the last run. Both commands are typed read operations available remotely. `tests/test_bridge_task_review.py`: 21 tests failed before the fix (for example `test_checkpoint_records_the_head_commit_and_files_already_modified` with `KeyError: head`). |
| CLIUX-004 | high | fixed | 0bde2f6. `control`/`console` explain the missing terminal; `usage` prints the table. `tests/test_bridge_cli_errors.py::test_control_without_a_terminal_explains_itself`, `test_usage_without_a_terminal_prints_the_usage_table` and three more; all failed before the fix. |
| CLIUX-005 | high | fixed | fc9c5fa. Root cause: under `LLM_SCHEDULER_USAGE_JSON(_FILE)` the controller used the injected capacity while F8 read the providers itself. F8 now uses the same snapshots, labels its source, warns on a difference from the controller, lists the rotation by default (`a` all), and reads `× empty` at 0%. `tests/test_bridge_usage_agreement.py::test_embedded_read_uses_the_capacity_the_controller_observes` and `test_an_exhausted_monthly_window_reads_empty_not_conserve` failed before the fix. |
| CLIUX-006 | high | fixed | bae8ed0. One label, `REVIEW` (stored value stays `VERIFYING`; JSON adds `status_label`); missions with a task in review show `◆ NEEDS YOU`; Agents and Activity say `exited 0 · awaiting review`; new `task approve` (`task verify --approve` hidden alias); a reopened mission becomes `CREATED`. `tests/test_bridge_review_vocabulary.py`: 11 of 12 failed before the fix. 90 golden files updated (header, home list, picker, Tasks status, help line). |
| CLIUX-007 | high | fixed | 0bde2f6. Did-you-mean for unknown commands, subcommands and options; internal commands hidden. `tests/test_bridge_cli_errors.py::test_unknown_command_suggests_the_closest_public_command` and eight more; failed before the fix on the argparse choice list. |
| CLIUX-008 | high | fixed | b137f3e. `--mode native|background|headless` on `mission create` and `start` (old flags hidden aliases); Background stays the scripted default, with the reason stated in help; the home form offers all three; JSON `config.mode`. `tests/test_bridge_mode_option.py::test_create_mode_option_sets_the_flags_and_json_mode` and six more; failed before the fix. |
| CLIUX-009 | high | fixed | b137f3e. Help defaults come from the resolving function; completed missions reopen the same way with or without `--mission`; `help keys` names all nine views; a refused command clears the line. `tests/test_bridge_mode_option.py::test_help_default_rotation_drops_providers_the_config_disables` and six more; failed before the fix. |
| CLIUX-010 | high | fixed | 82a36cf. `mission create` warns about an unfinished mission in the same folder (`--new` silences it); suggested commands include `--mission`; the ambiguity list shows when each mission was updated. `tests/test_bridge_cli_start_create_ux.py`::test_create_warns_when_the_folder_already_has_an_unfinished_mission and four more; failed before the fix. |
| CLIUX-011 | medium | fixed | /reject refuses a reason starting with `/`; Up/Down recall history while the command line has focus; the failure hint names Ctrl-P. `tests/test_bridge_cliux_console.py` |
| CLIUX-012 | medium | fixed | 4cf1bf0. Typing `/` shows a filtered list of commands with descriptions; Tab completes only a common prefix and a second Tab lists matches. `tests/test_bridge_cli_ux_console.py::test_tab_never_picks_the_first_of_several_matches` and `test_typing_slash_lists_commands_with_descriptions_and_filters_as_typed` failed before the fix; new goldens `console-slash-list*`. |
| CLIUX-013 | medium | fixed | 4cf1bf0. Footer `Esc close (draft kept)`, line count instead of bytes below 80 %, Ctrl-E restores the last add-task draft with its time. `tests/test_bridge_cli_ux_console.py::test_esc_keeps_the_draft_and_ctrl_e_reopens_it_with_its_time` and `test_composer_footer_labels_escape_and_counts_lines` failed before the fix. |
| CLIUX-014 | medium | fixed | Ctrl-D closes the composer and keeps the draft, Ctrl-X discards, F-keys only switch views; `help keys` lists the remaining exceptions. `tests/test_bridge_cliux_console.py`, `tests/test_bridge_composer_recovery.py` |
| CLIUX-015 | medium | fixed | `--json` errors go to stdout as `{"error": {code, message, hint}}`; every JSON list is an object; tasks carry `number`. `tests/test_bridge_cli_conventions.py` |
| CLIUX-016 | medium | fixed | Exit status 0-4 (success/no-op, runtime, usage, not found, wrong state), listed in `llm-bridge --help` and operations.md. `tests/test_bridge_cli_conventions.py` |
| CLIUX-017 | medium | fixed | Per-command `Effects:` line only for commands that change state; one example per page. `tests/test_bridge_help_ux.py` |
| CLIUX-018 | medium | fixed | Top-level help grouped by task with Global options and Exit status sections. `tests/test_bridge_help_ux.py` |
| CLIUX-019 | medium | fixed | Subcommands listed common-first with lowercase descriptions and one example each. `tests/test_bridge_help_ux.py` |
| CLIUX-020 | medium | fixed | 1f05142, 80f2676. `events` uses the Activity humaniser (moved to `bridge_event_text.py`) and hides bookkeeping unless `--verbose`; `command-rejected` records the reason; Health groups internals; Sessions and inspectors use plain words and omit unreported fields. `tests/test_bridge_events_cli.py`, `tests/test_bridge_plain_wording.py`; failed before the fix. |
| CLIUX-021 | medium | fixed | 80f2676. Plain messages for unreadable files, empty prompts and handoff without a route; a test checks CLI stderr and `raise` texts for `None`, `Errno` and `{}`. `tests/test_bridge_error_messages.py`, failed before the fix. |
| CLIUX-022 | medium | fixed | The station strip shows REVIEW as soon as the provider exits 0; the review message fits 80 columns. `tests/test_bridge_cliux_console.py` |
| CLIUX-023 | medium | fixed | `control` outside the station shows an `outside station · /attach` badge; stale notices are withdrawn; a finished mission exits to the shell with a one-line notice. `tests/test_bridge_cliux_console.py` |
| CLIUX-024 | medium | fixed | `pause --after-task` and `stop --after-task [N]` store the same intent as the steer kinds; `tests/test_bridge_cliux_batch3.py` |
| CLIUX-025 | medium | fixed | console reject dialog offers H Reject and hold; `tests/test_bridge_cliux_batch3.py` |
| CLIUX-026 | medium | fixed | `task amend` on a task in REVIEW is refused unless `--requeue`; a derived title follows the new prompt. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-027 | medium | fixed | 4cf1bf0. Help pages open word-wrapped with `↑↓ scroll · / find · Esc close` and no file-viewer controls; home `?` shows home keys (`T` opens the quickstart). Plain-text reflow instead of Markdown rendering, because the renderer merged command lines. `tests/test_bridge_cli_ux_console.py::test_help_pages_wrap_at_word_boundaries` failed before the fix. |
| CLIUX-028 | medium | fixed | Console `/help` is generated from the CLI command groups, with Views and a slash-to-shell table. `tests/test_bridge_help_ux.py` |
| CLIUX-029 | medium | fixed | Start/Resume is one context action; Attach and Leave are hidden for headless missions; a cut description is shown in full. `tests/test_bridge_cliux_console.py` |
| CLIUX-030 | medium | fixed | `start`, `steer`, `pause`, `stop`, `attach`, `help verification` and `help states` rewritten in task language; new `help terms`. `tests/test_bridge_help_ux.py` |
| CLIUX-031 | medium | fixed | 9b3dd51. Sign-in state on provider entries, one provider list for doctor and the form, F5 starts from any field, clearer rotation hint, subtitle lists installed providers and fits 80 columns. Alt-S was not used because Esc-prefixed keys conflict with the form. `tests/test_bridge_form_capacity.py`: 24 of 26 failed before the fix. |
| CLIUX-032 | medium | fixed | ASCII cut marker is `...`; picker folders are cut from the left; `missions`/`mission list` print an uncut table; `status` prints the full name. `tests/test_bridge_cliux_output.py` |
| CLIUX-033 | medium | fixed | `mission list` and `missions` print the same table (ID, STATE, NEEDS YOU, TASKS, NAME, FOLDER, UPDATED). `tests/test_bridge_cli_conventions.py` |
| CLIUX-034 | medium | fixed | A finished mission's Next row names what to do next on each surface. `tests/test_bridge_cliux_console.py` |
| CLIUX-035 | medium | fixed | `mission create --verifier 'CMD'` and `-- CMD` are accepted; the receipt shows the verifier argv. `tests/test_bridge_help_ux.py` |
| CLIUX-036 | low | fixed | `doctor --mission` shows the mission's Needs-you hint as Next. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-037 | low | fixed | Distinct ASCII state glyphs; UTF-8 cancelled is `⊘`. `tests/test_bridge_cliux_output.py` |
| CLIUX-038 | low | fixed | console header shows the short mission ID next to the name; `tests/test_bridge_cliux_batch3.py` |
| CLIUX-039 | low | fixed | `theme` says how to set the theme; `--palette-size` and `--colour` (old names kept as hidden aliases). `tests/test_bridge_help_ux.py` |
| CLIUX-040 | low | fixed | `steer` help shows `--kind {handoff,review}` and `--to`; errors name the missing piece with an example. `tests/test_bridge_help_ux.py` |
| CLIUX-041 | low | fixed | `worker enroll` and `remote login` accept `https://` or `wss://`; `--target` after the command shows the corrected command line. `tests/test_bridge_cli_conventions.py` |
| CLIUX-042 | low | fixed | `rotation --help` shows only `--set`. `tests/test_bridge_help_ux.py` |
| CLIUX-043 | low | fixed | Default mission name is the first line cut at a word boundary under 40 characters; the receipt names the folder. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-044 | low | fixed | 1f05142. `events --since AGE` and `--follow`; help says IDs are global. `tests/test_bridge_events_cli.py`, failed before the fix. |
| CLIUX-045 | low | fixed | `view PATH` works positionally. `tests/test_bridge_cli_conventions.py` |
| CLIUX-046 | low | fixed | Controller, Needs-you and Next sentences come from `bridge_wording.py` per surface (CLI or console). `tests/test_bridge_cliux_console.py` |
| CLIUX-047 | low | fixed | Health's Execution row reads the same operation row as Sessions. `tests/test_bridge_cliux_console.py` |
| CLIUX-048 | low | fixed | 0bde2f6. `missions --from` suppressed, `attach --client` reworded. `tests/test_bridge_cli_errors.py::test_missions_from_and_attach_client_help`, failed before the fix. |
| CLIUX-049 | low | fixed | Mission ID prefixes need 3 characters. `tests/test_bridge_cli_conventions.py` |
| CLIUX-050 | low | fixed | 4cf1bf0. Dimmed placeholder in an empty add-task composer, removed on the first keystroke. `tests/test_bridge_cli_ux_console.py::test_empty_composer_shows_a_dim_placeholder_until_the_first_keystroke` failed before the fix; new golden `overlay-composer-empty`. |
| CLIUX-051 | high | fixed | 1668bcb. Root cause: the operation was opened and marked RUNNING before the scheduler waited up to 900 s for capacity data, and the countdown showed the next poll. The wait now happens before the operation exists, with one absolute deadline. `tests/test_bridge_status_honesty.py::test_capacity_data_wait_is_not_a_running_operation_and_its_deadline_does_not_move`, failed before the fix. |
| CLIUX-052 | high | fixed | 82a36cf. The "Mission reopened" receipt now includes `--mission`. `tests/test_bridge_cli_start_create_ux.py`::test_suggested_commands_name_the_mission failed before the fix. |
| CLIUX-053 | high | fixed | 1668bcb. Root cause: `stop --force` reconciled the operation like a crash, so the task stayed BLOCKED. Force-stopped tasks are released on `start`; no invented reset time. `tests/test_bridge_status_honesty.py::test_a_task_interrupted_by_stop_force_is_released_by_start_but_a_crashed_one_is_not` and `test_start_after_stop_force_runs_the_interrupted_task_again`, failed before the fix. |
| CLIUX-054 | medium | fixed | 1668bcb. With no recorded provider PID, `stop --force` stops the controller and says no provider process was running. `tests/test_bridge_status_honesty.py::test_stop_force_with_no_provider_process_succeeds_and_says_so`, failed before the fix. |
| CLIUX-055 | high | fixed | 9b3dd51. The form reads capacity in the background: an exhausted provider is annotated with its reset, the Start hint says Start waits, and an automatic pick prefers a provider with capacity; doctor lists Copilot. `tests/test_bridge_form_capacity.py`, failed before the fix; new golden `home-form-start-waits`. |
| CLIUX-056 | medium | fixed | `task list` follows the scheduler order with RUNS, PRIORITY and ORDER columns and a legend. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-057 | medium | fixed | `task priority` and `task reorder` say where the task now runs; help states the sort direction. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-058 | medium | fixed | 1f05142. `events` shows the newest N events (printed oldest to newest) with a "showing the last N of M" line; `--after` keeps the forward cursor. `tests/test_bridge_events_cli.py`, failed before the fix. |
| CLIUX-059 | medium | fixed | The console action is `Leave` / `/leave`; CLI `detach` keeps its meaning and points to `/leave`. `tests/test_bridge_cliux_console.py` |
| CLIUX-060 | medium | fixed | The console capacity-wait row names `/rotation` and `/stop`. `tests/test_bridge_cliux_console.py` |
| CLIUX-061 | medium | fixed | A READY current task counts as Next and Now reads idle, in the console and CLI status. `tests/test_bridge_cliux_console.py` |
| CLIUX-062 | medium | fixed | 1668bcb. A RUNNING or PAUSED mission without a controller shows `INTERRUPTED` with the restart command; the `stale` word is gone. `tests/test_bridge_status_honesty.py::test_a_mission_with_no_controller_is_not_shown_as_plainly_running`, failed before the fix. |
| CLIUX-063 | medium | fixed | `attach`, `detach`, `finish` and `station` on a finished mission say it has no station. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-064 | medium | fixed | Exit statuses 75, 76, 130 and 143 are shown as words; `events` and `--json` keep the number. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-065 | medium | fixed | `LANG=C`/`LC_ALL=C` select ASCII; `LLM_BRIDGE_ASCII=1` forces it; `theme` shows the glyph set. `tests/test_bridge_cliux_output.py` |
| CLIUX-066 | medium | fixed | 9b3dd51. `doctor` exits 1 when no provider can run or the database check fails; JSON has `ready` and `blocking`. `tests/test_bridge_form_capacity.py`, failed before the fix. |
| CLIUX-067 | low | fixed | 9b3dd51. Objective hint `Type or paste the task · F5 start now · Tab more options · Ctrl-E full editor`. `tests/test_bridge_form_capacity.py`, failed before the fix. |
| CLIUX-068 | low | fixed | 9b3dd51. Empty state names only installed providers and wraps at word boundaries. `tests/test_bridge_form_capacity.py`, failed before the fix. |
| CLIUX-069 | low | fixed | The ASCII console header leaves out the identity bar. `tests/test_bridge_cliux_output.py` |
| CLIUX-070 | low | no change | Already behaves as the finding asks on this branch; regression test added in `tests/test_bridge_cliux_output.py`. |
| CLIUX-071 | low | fixed | Long-option abbreviations are disabled; explicit short aliases work. `tests/test_bridge_cli_conventions.py` |
| CLIUX-072 | low | fixed | List commands take `--limit` and end a partial list with `showing N of M`. `tests/test_bridge_cli_conventions.py` |
| CLIUX-073 | low | fixed | Times not from today carry a weekday or date; `events` prints full timestamps. `tests/test_bridge_cliux_console.py` |
| CLIUX-074 | low | fixed | One topic registry feeds `help`, `help --help`, the unknown-topic error and console `/help`. `tests/test_bridge_help_ux.py` |
| CLIUX-075 | low | fixed | `task verify` with no flags names `task approve` and `--evidence`. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-076 | low | fixed | Footers always keep `Ctrl-D exit` and `? help`; repeated hints removed. `tests/test_bridge_cliux_console.py` |
| CLIUX-077 | low | fixed | Bare `llm-bridge` without a usable terminal says why before the help and exits 2. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-078 | low | no change | Already behaves as the finding asks on this branch; regression test added in `tests/test_bridge_cliux_lifecycle.py`. |
| CLIUX-079 | low | fixed | `attach` to a stopped mission offers `control` first and `start` second. `tests/test_bridge_cliux_lifecycle.py` |
| CLIUX-080 | low | fixed | Task numbers work positionally (`task show 1`, `proposal accept 2`); `-m`, `-t`, `-j` short options. `tests/test_bridge_cli_conventions.py` |

## Dispositions

| Finding | Disposition |
| --- | --- |
| CLIUX-001 | fixed (82a36cf; tests/test_bridge_cli_start_create_ux.py) |
| CLIUX-002 | fixed (82a36cf; tests/test_bridge_cli_start_create_ux.py) |
| CLIUX-003 | fixed (075d38c; tests/test_bridge_task_review.py) |
| CLIUX-004 | fixed (0bde2f6) |
| CLIUX-005 | fixed (fc9c5fa) |
| CLIUX-006 | fixed (bae8ed0; tests/test_bridge_review_vocabulary.py) |
| CLIUX-007 | fixed (0bde2f6) |
| CLIUX-008 | fixed (b137f3e) |
| CLIUX-009 | fixed (b137f3e) |
| CLIUX-010 | fixed (82a36cf; tests/test_bridge_cli_start_create_ux.py) |
| CLIUX-011 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-012 | fixed (4cf1bf0) |
| CLIUX-013 | fixed (4cf1bf0) |
| CLIUX-014 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-015 | fixed (tests/test_bridge_cli_conventions.py) |
| CLIUX-016 | fixed (tests/test_bridge_cli_conventions.py) |
| CLIUX-017 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-018 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-019 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-020 | fixed (1f05142; tests/test_bridge_events_cli.py) |
| CLIUX-021 | fixed (80f2676; tests/test_bridge_error_messages.py) |
| CLIUX-022 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-023 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-024 | fixed (tests/test_bridge_cliux_batch3.py) |
| CLIUX-025 | fixed (tests/test_bridge_cliux_batch3.py) |
| CLIUX-026 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-027 | fixed (4cf1bf0) |
| CLIUX-028 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-029 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-030 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-031 | fixed (9b3dd51; tests/test_bridge_form_capacity.py) |
| CLIUX-032 | fixed (tests/test_bridge_cliux_output.py) |
| CLIUX-033 | fixed (tests/test_bridge_cli_conventions.py) |
| CLIUX-034 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-035 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-036 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-037 | fixed (tests/test_bridge_cliux_output.py) |
| CLIUX-038 | fixed (tests/test_bridge_cliux_batch3.py) |
| CLIUX-039 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-040 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-041 | fixed (tests/test_bridge_cli_conventions.py) |
| CLIUX-042 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-043 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-044 | fixed (1f05142; tests/test_bridge_events_cli.py) |
| CLIUX-045 | fixed (tests/test_bridge_cli_conventions.py) |
| CLIUX-046 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-047 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-048 | fixed (0bde2f6) |
| CLIUX-049 | fixed (tests/test_bridge_cli_conventions.py) |
| CLIUX-050 | fixed (4cf1bf0) |
| CLIUX-051 | fixed (1668bcb) |
| CLIUX-052 | fixed (82a36cf; tests/test_bridge_cli_start_create_ux.py) |
| CLIUX-053 | fixed (1668bcb) |
| CLIUX-054 | fixed (1668bcb) |
| CLIUX-055 | fixed (9b3dd51; tests/test_bridge_form_capacity.py) |
| CLIUX-056 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-057 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-058 | fixed (1f05142; tests/test_bridge_events_cli.py) |
| CLIUX-059 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-060 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-061 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-062 | fixed (1668bcb) |
| CLIUX-063 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-064 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-065 | fixed (tests/test_bridge_cliux_output.py) |
| CLIUX-066 | fixed (9b3dd51; tests/test_bridge_form_capacity.py) |
| CLIUX-067 | fixed (9b3dd51; tests/test_bridge_form_capacity.py) |
| CLIUX-068 | fixed (9b3dd51; tests/test_bridge_form_capacity.py) |
| CLIUX-069 | fixed (tests/test_bridge_cliux_output.py) |
| CLIUX-070 | no change (see Fix status) |
| CLIUX-071 | fixed (tests/test_bridge_cli_conventions.py) |
| CLIUX-072 | fixed (tests/test_bridge_cli_conventions.py) |
| CLIUX-073 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-074 | fixed (tests/test_bridge_help_ux.py) |
| CLIUX-075 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-076 | fixed (tests/test_bridge_cliux_console.py) |
| CLIUX-077 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-078 | no change (see Fix status) |
| CLIUX-079 | fixed (tests/test_bridge_cliux_lifecycle.py) |
| CLIUX-080 | fixed (tests/test_bridge_cli_conventions.py) |
