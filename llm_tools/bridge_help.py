"""Offline help shipped in the Python package and shared with the terminal UI."""
from __future__ import annotations

import textwrap

TOPICS: dict[str, str] = {
    "quickstart": """LLM Bridge — supervise AI work in your terminal

Needs: Python 3.11+, tmux, and one provider CLI that is installed and signed in.
Check with llm-bridge doctor; its Next line says what to do.

First mission, from your project folder. Opening Bridge starts no provider.

1. Run llm-bridge, then press N.
2. Type the task. The form selects the first provider that is installed and signed in.
3. Press F5 (start from any field), or Tab five times to reach "Start and open provider"
   and press Enter. This uses paid capacity. Claude Code may first ask whether to trust the folder: choose
   "Yes, I trust this folder" (the first option is No, exit; if you pick it, Bridge holds
   the task until you run /start again).
4. When the provider says it is done, exit it with /exit. The station then says the
   task is waiting for your review.
5. Press Ctrl-] then B, type /approve, press Enter, then y. The mission shows COMPLETED.
6. Press Ctrl-] then d to leave the station, and Ctrl-D to leave the controls.

The same from a script, with no terminal attached (run inside the mission's folder,
or add --mission ID):

  llm-bridge mission create --file task.md --name demo --mode native
  llm-bridge start                        starts provider work; may consume paid capacity
  llm-bridge status                       after the provider exits, "Needs you" shows the approve command
  llm-bridge task approve --task 1 --evidence 'tests passed'

A zero provider exit means the task is in REVIEW, not completed work. Review the
evidence before approving. Reopen a station with llm-bridge attach. Add more work
with llm-bridge task add --file next-task.md. Ctrl-] B opens Bridge and Ctrl-] P
returns to the same provider process.

Three modes, chosen with --mode native|background|headless on mission create
and start. Native keeps the provider's own interactive terminal in a tmux
station. Background runs the provider without a terminal under a background
controller and shows its activity log in a tmux station. Headless runs the
provider without a terminal, with no station and no tmux. Background is the
default for the command line because it needs no terminal, so scripts behave the
same everywhere; the home form offers all three and starts on Native, which is
recommended when you work interactively. status shows a mission's mode.
A new mission rotates over the providers (or routes) set in your llm-tools config
([ralph] wins over [defaults]), limited to the providers enabled in [usage]; with
neither it uses the built-in list. With your configuration that is @@DEFAULT_ROTATION@@.
--providers or --routes override either.

New missions use provider-native team guidance (teams on) and the Medium token
spend preference by default. Set --teams on|off and --token-spend low|medium|high
on create, or change a saved mission with mission set --teams/--token-spend. Older
missions without saved policy keep their original prompts. See help mission create.

Typing in the Bridge command line: bare commands and Codex/Claude-style slash
commands are the same: /start, /attach, /pause, /status and /approve [N] work
like their bare forms (N is the task to approve; it may be omitted when exactly
one task is in REVIEW). /help and ? show help.

Detach your own terminal with Ctrl-] then d, which works the same locally and
over SSH. Reattach with llm-bridge attach. The command llm-bridge detach is
different: it removes a station client from outside, normally another terminal's
(for example one still open at the machine while you are on SSH). Do not use it
to leave your own session. Detaching or exiting Bridge does not stop the mission.
Pause waits until the provider finishes its current step and never suspends this computer.
Capacity waits stay awake by default; `mission set --suspend-when-rate-limited
on` opts in to RTC-backed sleep at an idle boundary with no active operation.
Pause, stop and cancel changes made during sleep take effect after the armed
wake. Stop and cancel are separate explicit operations.

More help topics: @@TOPIC_LIST@@ (llm-bridge help lists them with descriptions).
Every public command also has --help. ID and task numbers are placeholders.
--mission accepts a unique ID prefix (4+ characters) and may be omitted when you
run the command inside the mission's folder.""",
    "terms": """Words Bridge uses

Mission      One piece of work in one folder: your objective plus an ordered list of
             tasks. Missions are saved, so closing the terminal does not lose them.
Task         One step of a mission. Each task has a number (1, 2, ...) and a state:
             waiting, running, in REVIEW (waiting for you), completed or cancelled.
Provider     The AI command-line tool that does the work, for example Claude Code,
             Codex or Copilot. A route is a provider pinned to one model.
Rotation     The ordered list of providers a mission may use. When one runs out of
             capacity, Bridge moves on to the next. Change it with llm-bridge rotation.
Controller   The background process that runs a mission: it starts the provider,
             watches capacity and moves from task to task. llm-bridge start starts it;
             llm-bridge stop ends it.
Station      The terminal window (a private tmux session) where you watch or use a
             provider. llm-bridge attach opens it. Leaving it does not stop the mission.
Verify       Check that a finished task is really done: read its output and diff, run a
             verifier command if the mission has one, then approve or reject the task.
Capacity     How much of a provider's usage allowance is left (the 5h, week and month
             windows shown by llm-bridge usage).

See help quickstart for the first mission, and help states for what each state means.""",
    "keys": """Keys and terminal ownership

Provider focus: ordinary keys, slash commands, arrows, Tab, Escape, Ctrl-C,
Ctrl-D, paste and requested mouse/focus events belong to the provider.
Ctrl-] is Bridge's prefix: b/B opens controls, p/P returns to Provider, ? shows
station keys, z toggles zoom, d detaches only your own terminal (the safe choice
over SSH). Ctrl-] Ctrl-] sends a literal prefix. Switching panes returns to the
same process; it does not launch another provider.

llm-bridge detach --mission ID is for another shell: it detaches the station's
single connected writable client, which is usually a different terminal from the
one running the command. With no client attached it reports that; with several
attached (for example local and SSH) it refuses and asks for --client /dev/pts/N.
Observers (read-only clients) are never detached.

Command line: slash commands such as /start, /attach, /approve [N] and /help are
accepted exactly like the bare commands; Escape returns focus without side effects.

Bridge body: F1-F9 select Overview, Tasks, Providers, Activity, Sessions, Health,
Actions, Usage, Agents. Line 1 is a live status strip (provider, task, inactivity,
capacity). Typing a view name (overview, tasks, providers, activity, sessions, health,
actions, usage, agents) switches to it; the older names orders, stations, log, systems and
palette still work. Tab and Shift-Tab cycle views. Up/Down select; PgUp/PgDn page; Home/End select endpoints.
Enter opens the labelled selection. ? opens context help.
Overview o opens the complete objective; Tasks v opens the complete prompt. Printable text starts
a command; Ctrl-G focuses the retained draft; click the input to focus it.

Command input: Left/Right move; Home/End or Ctrl-A/Ctrl-E move to endpoints.
Backspace/Delete remove adjacent text. Ctrl-U clears. After a command is refused or fails the line is empty and the command is in history; Ctrl-P recalls it.
Ctrl-P/Ctrl-N, or Up/Down while the command line has focus, recall history and restore the pre-history draft. Tab completes.
Enter submits an explicit command. ? is ordinary input. Escape returns to the
body with the draft retained. Ctrl-C cancels local editing first. Ctrl-D exits
only with empty input and no unresolved mutation; exit does not stop work.

Body Ctrl-E composes/adds a task; Tasks body Ctrl-R amends the selected task.
Ctrl-K opens Actions from any view with the filter ready. Activity: Ctrl-T hides or shows controller bookkeeping rows. Body Ctrl-F opens Tasks search; Enter applies,
Escape cancels and restores the command draft. Clear the query to show all tasks.
Actions lists what applies to the current state, with the command for each; / filters them. Single-click selects; Enter/double-click
activates the selected action. A description that does not fit its row is shown in full on the last row of the list. Disabled actions explain their unavailable target.

Composer: Enter newline; Tab indent; PgUp/PgDn page; Ctrl-S saves. Ctrl-O opens VISUAL/EDITOR.
Escape or Ctrl-D closes and keeps the draft; Ctrl-E in the console reopens it. Ctrl-X discards it.

Keys that mean different things in different places (the only exceptions):
  Key      Console body              Command input                 Composer
  Ctrl-D   exit the console          exit when the line is empty   close, draft kept
  Ctrl-E   add a task                move to end of line           (none)
  Ctrl-K   open Actions              open Actions                  kill to end of line
  Ctrl-R   amend selected task       (none)                        (none)
  Ctrl-S   (none)                    (none)                        save
  Ctrl-X   (none)                    (none)                        discard the draft
  Up/Down  select a row              recall history                move the caret
  o v l i  single-key view actions   typed as text                 typed as text
F-keys only switch views, everywhere. Printable text in the body starts a command.
Viewer: Up/Down, PgUp/PgDn or Space page; Home/End go first/last; / searches,
n/N visit matches; Escape cancels search before closing, q closes the viewer.
m toggles source/render, w toggles wrap, Left/Right scroll unwrapped lines.
h lists headings; s exports source to a new path; e edits an eligible task;
c copies source when a supported local helper is available.

Native application scrolling and tmux scrollback differ. With mouse capture,
Shift-drag selection depends on the terminal. Ctrl-] [ enters tmux copy mode;
q leaves copy mode. Use terminal paste shortcuts.

Clipboard: the station sets tmux set-clipboard external. Text you copy in tmux
copy mode is sent to your local clipboard with the OSC 52 escape sequence,
including over SSH, in terminals that support OSC 52 (many do; some need it
enabled). An application in the provider pane cannot set your clipboard this
way: its own OSC 52 sequences are ignored, so a provider cannot overwrite what
you paste next. Without OSC 52 support nothing reaches the clipboard; use
Shift-drag selection or export. The Bridge viewer's c copy uses a local helper
(wl-copy, xclip, xsel or pbcopy) and is separate from this.""",
    "console": "@@CONSOLE_HELP@@",  # generated from COMMAND_GROUPS by console_help()
    "remote": """Remote use and shared windows

Missions and provider stations belong to your user account, so you can log in
over SSH, run llm-bridge, and attach to a mission that is running at the machine.
Several terminals can attach at once. tmux sizes the shared window to the most
recently active client (window-size latest), so a second client of a different
size resizes the first one's view while it is in use. To avoid that, detach the
other client (Ctrl-] then d in that terminal, or llm-bridge detach --mission ID
from a shell), or use terminals of the same size.
Ctrl-] then d detaches only your own terminal and is safe over SSH. Text you
copy in tmux copy mode (Ctrl-] [) reaches your local clipboard through OSC 52
when your terminal supports it. See also: help keys.""",
    "distributed": """Work on other machines through a coordinator

Distributed use is optional; without it Bridge opens no network connection.
A coordinator (a separate service, see docs/distributed/install.md) relays
commands between this CLI, a browser and workers. A worker is a machine that
runs missions and connects out to the coordinator.

On a worker (once):
  llm-bridge worker enroll --coordinator wss://HOST:8443 --name build-1 --token-file T --ca-file ca.pem
  llm-bridge worker install-service      (or: llm-bridge worker run)
A worker is read-only unless enrolled with --allow-control. Terminals are shared
only with --allow-terminal-view or --allow-control. llm-bridge worker audit
shows what remote clients did on this worker.

On the machine you type on (once):
  llm-bridge remote login --coordinator https://HOST:8443 --token-file T --ca-file ca.pem
  llm-bridge worker list                 workers, connection state and last seen

Then put --target WORKER before a command, for example:
  llm-bridge --target build-1 mission list
  llm-bridge --target build-1 task add --mission ID --file prompt.md
The worker applies the same checks as a local command. A refused or offline
worker is reported with the reason; an outcome that cannot be confirmed is
reported as uncertain, never as success.""",
    "prompts": """Compose, import, amend and recover complete prompts

A 30 KiB or larger document belongs in the multiline composer or a file, not a
quoted command. In the home Objective field, open the composer with Ctrl-E.
Enter inserts a newline; Ctrl-S saves back to the form without starting work.
Escape or Ctrl-D retains a draft; Ctrl-X discards it. Check byte/line count and
tail before saving. The interactive ceiling is 1 MiB of encoded UTF-8 source,
not a provider context limit. Oversize transactions must leave prior text intact.

Bracketed paste is a single transaction; no command/navigation occurs before its
terminator. Without bracketed paste a terminal cannot reliably distinguish paste
from typing: use the multiline composer, where Enter always inserts a newline.
Do not paste multiline documents into the short command input. Complex emoji
shaping depends on the terminal; common wide and combining characters are handled.

Import without the interactive ceiling:
  llm-bridge mission create --name demo --mode native --file prompt.md --cwd .
  llm-bridge task add --mission ID --file prompt.md --acceptance 'tests pass'
  llm-bridge task amend --mission ID --task 1 --file revised.md
`--file -` reads stdin in the standalone CLI; the interactive console cannot read
its own terminal as a pipe. --json changes output formatting, not source text.

Files/editor round trips retain source line endings, indentation, repeated spaces
and final newlines. Terminal paste preserves logical source; terminal newline
translation is distinct from file byte preservation. Viewing or wrapping never
changes source. Existing larger records remain readable/exportable.

Editor: set VISUAL or EDITOR to an executable and arguments. No prompt is executed
as shell text. Nonzero editor exit, failed save or late store rejection must retain
the private draft. Use the recovery path shown in feedback; export it before
retrying or creating a new mission. Cancelled missions cannot accept new tasks;
a completed one is reopened by the new task.
Final authoritative validation still occurs when a durable mutation is applied.""",
    "markdown": """Read full source and Markdown

In Tasks, open the selected full prompt with v. Bounded status previews are not
the complete source. The viewer identifies document kind and source/render mode.
Use / to search the whole document, n/N for matches, Home/End and PgUp/PgDn to
navigate, m for source/render, w for wrap, Left/Right for long unwrapped code.
Closing returns to the prior Bridge context. Source export always uses original
text, without rendered wrapping, selection marks or line numbers.

Markdown is local text presentation: headings, lists, quotes, fences and tables.
Rich is installed with Bridge and loaded only for rendering; no browser or
external renderer is required. No code executes, images download or links open.
Unsupported syntax and malformed fences remain readable. If rendering fails,
plain source stays available and the degraded mode is labelled. NO_COLOR removes
color; text and focus cues remain. Provider output is never parsed as Markdown.

Use viewer export to choose an explicit destination. When a supported local
clipboard helper is available, Copy source copies the complete source. Export
is the reliable fallback; the viewer itself never sends clipboard escape sequences.
Retained provider logs are bounded; pruned content cannot be recovered by paging.
Open an artifact with llm-bridge view --file notes.md. Add --follow only to
follow changes: scrolling up pauses, End resumes, and rotation/truncation is
labelled. Without a TTY, view prints complete source; --follow requires a TTY.""",
    "providers": """Providers, routes and native stations

Native supervises a real CLI in a private owned tmux station. Background (--mode
background, the command-line default) runs the provider without a terminal and shows
its activity log in the station; Headless (--mode headless) runs it with no station
and no tmux.
Opening, inspecting and attaching start no paid work. start/resume can run work.
Selecting a station changes the viewed terminal, not routing of an invocation.
Use configured providers/routes and safe-boundary steering for future launches.
A configured model pin is not proof of the model observed running.

Only a verified exact native session identity can be resumed. Otherwise Bridge
reconstructs context; a guessed ID is never accepted as proof of ownership.

Missing tmux: install it using your system package manager, or choose headless.
Missing CLI: install/configure that CLI, then inspect status. Authentication:
complete the provider's normal login locally; never paste credentials into a
mission. Capacity waits: inspect Usage or llm-usage --show-source, then wait for
the real reset or choose an eligible route at a safe boundary. Usage refresh is
background work; it must not take ownership of the native provider terminal.""",
    "states": """Desired state, running work and boundaries

Mission state is persisted intent; observed operations show what is executing.
Pause means "pausing after the current step": the provider keeps working until
its current step ends, then the mission holds. Pause never suspends this computer.
A provider shows ACTIVE only while it is actually working; "Last selected" is a
record of what was chosen, not of what is running.
Completed missions have no active execution. Adding a task to one (task add, with or
without --mission) reopens it as STOPPED and says so; start continues it. Cancelled
missions cannot accept tasks.

Provider zero exit enters verification. Passing required verification and operator
approval where configured establish completion. A native mission then holds
before its next task until you /approve or /reject the reviewed one, or run
/continue to start the next task while it keeps waiting (mission create
--auto-continue, or mission set --auto-continue on, turns the hold off). Blocked means a dependency,
verification request or uncertain operation needs attention: inspect status/events.
Stop ends Bridge's controller for this mission; --force also ends a provider
that does not exit by itself. Cancel marks the mission or task as cancelled and
keeps its record. Neither affects other missions or your other terminals. Exiting Bridge or detaching
only leaves the UI; it does not cancel or stop the controller.

Changes that wait for the next safe point, for example a handoff:
  llm-bridge steer --mission ID --kind handoff --to codex
The command only queues the change. "Queued" does not mean the change has taken
effect yet: the console shows submitting, accepted, deferred, applied or rejected,
and status shows the current state.""",
    "verification": """Verify before declaring completion

A provider that exits without an error has not necessarily done the work. Inspect task source, evidence and
acceptance conditions; use the actual mission and task IDs:
  llm-bridge task show --mission ID --task 1 --json
  llm-bridge task output --mission ID --task 1        the provider's output of the last run
  llm-bridge task diff --mission ID --task 1          the files that run changed in the working tree
  llm-bridge task verify --mission ID --task 1 --evidence 'pytest: 12 passed'
  llm-bridge task approve --mission ID --task 1 --evidence 'reviewed'
Approving marks the task as completed. In the console, approving asks you to
confirm which task you are approving; on the command line it does not ask.
In the console the approval dialog lists the changed files; o opens the output and
d the diff in the pager. task output pages the log on a terminal (--follow keeps
following a growing log) and prints it plainly when piped; a log pruned for age
is reported with its recorded size and digest. task diff compares the working
tree with the commit and modified files recorded when the run started.

Set a verifier when you create the mission. It is a command that checks a finished
task; it is split into words like a shell would, but it is not run through a shell:
  llm-bridge mission create --file prompt.md --verifier 'python -m pytest -q'
The same with the command last, after --:
  llm-bridge mission create --file prompt.md -- python -m pytest -q
The receipt prints the command as a list of arguments. For arguments that need exact
quoting, repeat --verifier-arg once per argument (--verifier-arg=-m for one starting
with a dash). Default verifier timeout is 300 seconds. A verifier may use {context}, {cwd},
{mission_id}, {task_id}, {revision}. No verifier is added implicitly. Verification
may execute the configured local command; task verify itself does not start a
paid provider. Inspect failed evidence and correct the task before retrying.""",
    "recovery": """Recover without losing source or replaying mutations

Rejected edit: retain/open the private draft path shown, export it, correct the
eligibility/problem, then explicitly retry. A mission completed during editing
cannot accept that task; save the source for another mission. Editor failure:
fix VISUAL/EDITOR and reopen the retained file. Drafts use private files and are
not automatically expired. Never put full prompts into ordinary operational logs.

Unknown outcome or lost acknowledgement: inspect status and events first:
  llm-bridge status --mission ID --json
  llm-bridge events --mission ID --limit 20 --json
A timeout does not prove rollback. Do not blindly repeat start/add/cancel/approve.
If another command draft is being edited, a late result must not overwrite it.

Disconnected terminal: llm-bridge attach --mission ID reopens an existing owned
station. Inspect before starting another controller. Stale observation: refresh
status and use doctor; do not treat the last station selection as active work.
Database busy: wait for the active writer and inspect status before retrying.
Missing CLI/tmux or login: fix the local prerequisite; see help providers.
Broken renderer: use plain source. Help needs neither Rich, tmux nor mission state.
Stop/cancel have explicit scopes; detach and exit keep the mission separate.""",
}


HOME_HELP = """Home screen: your saved missions

You are already here, so the quickstart's first step is done. Opening a mission or the
form below starts no provider; only S (start) and "Start and open provider" do.

N              new mission: type the task, then Save or Start
Enter          open the selected mission's controls (attach, when its provider is live)
o              open the controls of the selected mission
S              start the selected mission, when it has not started
Up/Down        choose a mission (j and k also work)
/              filter the list by name or folder
?              this page
Q or Esc       leave Bridge

Nothing to start yet? T opens the step-by-step quickstart; the same text is
llm-bridge help quickstart."""


_VIEW_HELP_FOOTER = "Every key: /help keys · every slash command: /help · Esc closes this page"

_VIEW_HELP: dict[str, str] = {
    "Overview": """Overview: what the mission is doing and what needs you

Reading it: Controller (is the mission being driven), Now and Next (tasks),
Needs you (approvals, proposals, blocked tasks), Capacity, then Recent events.
Keys: o read the full objective · Ctrl-E add a task · Ctrl-K find an action
Slash: /start /pause /resume /attach /status""",
    "Tasks": """Tasks: the ordered work of this mission

Keys: Up/Down choose · Enter inspect · v read the full prompt · Ctrl-R amend
      Ctrl-E add a task · Ctrl-F find in task text (Esc clears the filter)
Slash: /approve N · /reject N WHY · /task cancel --task N
A task waiting for your decision shows REVIEW; approve it only after reading its evidence.""",
    "Providers": """Providers: who works and how much capacity is left

Each block is one provider or route: its live state, the 5h / week / month
windows and its latest operations. Before the first run the numbers come from
the Usage data.
Keys: Up/Down choose · Enter mark as viewed (changes the shown terminal only)
Slash: /attach opens the provider terminal""",
    "Activity": """Activity: what happened, newest last

Operator-level events are plain; controller bookkeeping is dim.
Keys: PgUp older · PgDn newer · Ctrl-T hide or show bookkeeping rows
Slash: /status prints the current state""",
    "Sessions": """Sessions: provider operations and their native session ids

One row per operation: task, provider, model, status, native session and whether
it could be restored. Keys: Up/Down scroll.""",
    "Health": """Health: controller, persistence and capacity diagnostics

Mode, lifecycle, controller heartbeat, execution, health and capacity sources.
Keys: Up/Down scroll · Shift-Left/Right pan. Use /status for a printable summary.""",
    "Actions": """Actions: what you can do now

The list follows the mission state; the command to type is shown for each.
Keys: Up/Down choose · Enter run (risky ones ask first) · Ctrl-K find an action
Slash: typing / followed by a letter goes to the command line, so
/reject 3 WHY works here too.""",
    "Usage": """Usage: the llm-usage dashboard for the mission's providers

The table comes first. It lists the mission's rotation providers; a shows every
installed provider. If a live read differs from the controller's last observation, a
line under the title says so. Keys: l show or hide the legend · Up/Down scroll
Shift-Left/Right pan wide tables. At narrow widths the Remaining Time column is
hidden before anything else. Slash: /providers shows the capacity per provider.""",
}


def view_help(view: str) -> str:
    """Short help (under one screen) for one console view, ending with a link to the full topics."""
    body = _VIEW_HELP.get(view)
    if body is None:
        body = f"{view}\n\nKeys: Up/Down scroll."
    return f"{body}\n\n{_VIEW_HELP_FOOTER}"


def _command_path_parser(key: str):
    """The argparse parser for a command path such as ``task verify``, or ``None``."""
    # Import the grammar only when command-path help is requested. No database,
    # renderer or terminal service is needed to format argparse help.
    from .bridge import _build_parser
    import argparse
    parser = _build_parser()
    for word in key.split():
        groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if not groups or word not in groups[0].choices or word in {"console", "_controller"}:
            return None
        parser = groups[0].choices[word]
    return parser


def is_known_topic(topic: str) -> bool:
    """Whether ``topic`` is a help topic or a command path."""
    key = " ".join(topic.strip().lower().split())
    return key in TOPICS or _command_path_parser(key) is not None


# One list of help topics, in the order every list shows them; each has a one-line description.
# `help`, `help --help`, the unknown-topic error, the quickstart and the console /help all read it.
TOPIC_SUMMARIES: dict[str, str] = {
    "quickstart": "your first mission, step by step",
    "terms": "what mission, task, station, controller and the other words mean",
    "keys": "keys, Ctrl-] and which terminal belongs to whom",
    "console": "the command line at the bottom of Bridge and its slash commands",
    "prompts": "write, import, amend and recover long prompts",
    "markdown": "read full prompts and Markdown documents",
    "providers": "providers, routes and native stations",
    "states": "what paused, running, blocked and active mean",
    "verification": "check finished work, set a verifier, approve or reject",
    "recovery": "what to do after an error, a lost terminal or an unknown result",
    "remote": "use Bridge over SSH and share a window",
    "distributed": "run missions on other computers through a coordinator",
}


def topic_names() -> tuple[str, ...]:
    """Every help topic name, in display order."""
    return tuple(TOPIC_SUMMARIES)


def topic_summary(name: str) -> str:
    return TOPIC_SUMMARIES[name]


def topic_list_text() -> str:
    """The topic names on one line, for prose and option help."""
    return ", ".join(topic_names())


def topic_index() -> str:
    """The topics as a table with their descriptions (``llm-bridge help``)."""
    width = max(len(name) for name in TOPIC_SUMMARIES)
    rows = [f"  {name:<{width}}  {summary}" for name, summary in TOPIC_SUMMARIES.items()]
    return "Help topics (llm-bridge help TOPIC)\n" + "\n".join(rows) + "\n\nCommand pages: llm-bridge help COMMAND, for example llm-bridge help task approve."


def topic_text(topic: str = "quickstart") -> str:
    key = " ".join(topic.strip().lower().split())
    if key in TOPICS:
        text = TOPICS[key]
        if "@@DEFAULT_ROTATION@@" in text:
            from .bridge import resolved_default_rotation

            providers, routes = resolved_default_rotation()
            text = text.replace("@@DEFAULT_ROTATION@@", f"routes {routes}" if routes else f"providers {providers}")
        if "@@CONSOLE_HELP@@" in text:
            text = text.replace("@@CONSOLE_HELP@@", console_help())
        return text.replace("@@TOPIC_LIST@@", ", ".join(name for name in topic_names() if name not in {"quickstart", "terms", "console"}))
    parser = _command_path_parser(key)
    if parser is None:
        return f"Unknown help topic '{topic.strip() or '(empty)'}' · topics: {topic_list_text()} · or a command such as: task verify"
    return parser.format_help()


# The commands of the top-level page and of the console /help, grouped like `gh`. Each entry is
# (command words, one-line description). A test checks that every public top-level command is here.
COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Start here", (
        ("doctor", "check this computer and say what to do next"),
        ("help quickstart", "your first mission, step by step"),
        ("help", "list every help topic"),
    )),
    ("Missions", (
        ("mission create", "save a mission and its first task"),
        ("mission list", "list saved missions"),
        ("mission set", "change a mission setting"),
        ("missions", "every mission, live ones first; switch between them"),
        ("status", "what the mission is doing and what needs you"),
    )),
    ("Tasks", (
        ("task add", "add a task to the mission"),
        ("task list", "list the mission's tasks"),
        ("task show", "read a task's full text and evidence"),
        ("task output", "read what the provider printed on its last run"),
        ("task diff", "files the last run changed in the working tree"),
        ("task amend", "replace a task's text"),
        ("task approve", "accept a task that waits in REVIEW"),
        ("task reject", "send a task back with a reason"),
        ("proposal", "review tasks that a provider suggests"),
    )),
    ("Run control", (
        ("start", "start working on the mission's tasks"),
        ("pause", "hold after the provider's current step"),
        ("resume", "continue after a pause or a stop"),
        ("continue", "start the next task without approving the last one"),
        ("finish", "ask the provider to exit so its task moves to review"),
        ("stop", "stop the mission's controller"),
        ("cancel", "cancel the mission's unfinished tasks"),
        ("steer", "hand over to another provider or add a review"),
    )),
    ("Station", (
        ("attach", "open the provider terminal"),
        ("detach", "disconnect another terminal from the station"),
        ("station", "choose which provider's terminal the controls show"),
        ("control", "open the full control view"),
        ("agents", "live provider operations and native child agents"),
    )),
    ("Capacity and power", (
        ("usage", "capacity left for each provider"),
        ("rotation", "show or change the providers a mission uses"),
        ("suspend-until-credits", "suspend this computer until capacity returns"),
    )),
    ("Data", (
        ("events", "recent mission events"),
        ("view", "read a text file in the pager"),
        ("backup", "copy the Bridge database to a file"),
        ("restore", "replace the Bridge database from a backup"),
        ("gc", "close the idle stations of finished missions"),
    )),
    ("Distributed", (
        ("remote", "reach other computers through a coordinator"),
        ("worker", "let a coordinator use this computer"),
    )),
    ("Appearance", (
        ("theme", "show the colour theme and a swatch per role"),
        ("mission colour", "pin the mission's colour, or return to automatic"),
    )),
)

ROOT_GLOBAL_OPTIONS = (
    ("--version", "print the installed version and exit"),
    ("--target WORKER", "run the command on another computer through the coordinator (help distributed)"),
    ("-h, --help", "show this page; llm-bridge COMMAND --help shows one command"),
)

# Extra sections appended after the global options on the top-level page, each already formatted
# and separated from the previous one by a blank line (for example "Exit status").
ROOT_HELP_TRAILER = ""


def _rows(entries: tuple[tuple[str, str], ...], indent: str = "  ") -> list[str]:
    width = max(len(name) for name, _ in entries)
    return [f"{indent}{name:<{width}}  {text}" for name, text in entries]


def root_help() -> str:
    """The top-level ``llm-bridge --help`` page."""
    lines = [
        "usage: llm-bridge [--target WORKER] [COMMAND] [ARGS]",
        "",
        "Supervise AI work in your terminal. Run llm-bridge with no command to open the",
        "home screen: your missions and a form for a new one. Opening it starts no provider.",
    ]
    names = [name for _, entries in COMMAND_GROUPS for name, _ in entries]
    width = max(len(name) for name in names)
    for title, entries in COMMAND_GROUPS:
        lines += ["", title]
        lines += [f"  {name:<{width}}  {text}" for name, text in entries]
    lines += ["", "Global options"]
    lines += _rows(ROOT_GLOBAL_OPTIONS)
    text = "\n".join(lines) + "\n\nllm-bridge COMMAND --help shows one command; llm-bridge help lists the topics.\n"
    if ROOT_HELP_TRAILER:
        text += "\n" + ROOT_HELP_TRAILER.strip("\n") + "\n"
    return text


# Console views: (slash name, key, one-line description).
_CONSOLE_VIEWS = (
    ("overview", "F1", "what the mission is doing and what needs you"),
    ("tasks", "F2", "the ordered tasks"),
    ("providers", "F3", "who works and how much capacity is left"),
    ("activity", "F4", "what happened, newest last"),
    ("sessions", "F5", "provider operations and their session ids"),
    ("health", "F6", "controller, storage and capacity diagnostics"),
    ("actions", "F7", "what you can do now"),
    ("usage", "F8", "the llm-usage dashboard"),
)

# Slash shortcuts that take plain arguments, with the shell command each one runs.
_CONSOLE_SHORTCUTS = (
    ("/approve [N]", "llm-bridge task approve --task N"),
    ("/reject [N] WHY", "llm-bridge task reject --task N --reason WHY"),
    ("/verify N EVIDENCE", "llm-bridge task verify --task N --evidence EVIDENCE"),
    ("/retry N", "llm-bridge task retry --task N"),
    ("/pause N, /resume N, /cancel N", "llm-bridge task pause --task N (and resume, cancel)"),
    ("/add FILE", "llm-bridge task add --file FILE"),
    ("/amend N FILE", "llm-bridge task amend --task N --file FILE"),
    ("/proposals [STATUS]", "llm-bridge proposal list --status STATUS"),
    ("/home", "llm-bridge missions --pick (the mission picker)"),
)


def console_help() -> str:
    """The console ``/help`` page, generated from :data:`COMMAND_GROUPS`."""
    view_names = {name for name, _, _ in _CONSOLE_VIEWS}
    lines = [
        "Bridge console: commands at the bottom line",
        "",
        "Type at the bottom line. The mission is implied, bare words work like slash",
        "commands (start = /start), and Tab completes.",
    ]
    for title, entries in COMMAND_GROUPS:
        shown: list[tuple[str, str]] = []
        for name, text in entries:
            if name == "help quickstart":
                continue
            if name == "help":
                shown.append(("/help [TOPIC]", "a help topic; /help terms explains the words used here"))
                continue
            if name in view_names:
                continue  # listed once, under Views
            if name == "detach":
                # In the console /detach is an old name for /leave, which leaves your own terminal.
                shown.append(("/leave", "leave the provider terminal; work keeps running"))
                continue
            shown.append(("/" + name, text))
        lines += ["", title]
        lines += _rows(tuple(shown))
    lines += ["", "Help topics", *textwrap.wrap(topic_list_text(), 76, initial_indent="  ", subsequent_indent="  ")]
    lines += ["", "Views (open with the slash command or the F key)"]
    lines += _rows(tuple((f"/{name} ({key})", text) for name, key, text in _CONSOLE_VIEWS))
    lines += [
        "",
        "Slash commands and shell commands",
        "  /NAME ARGS is llm-bridge NAME ARGS with the mission filled in, so /task list",
        "  runs llm-bridge task list. These shortcuts take plain arguments instead of flags:",
    ]
    lines += _rows(_CONSOLE_SHORTCUTS, indent="    ")
    lines += [
        "",
        "Keys: Ctrl-E add a task, Ctrl-R amend the selected task, Ctrl-K find an action,",
        "? explains the current view, Ctrl-D leaves (the mission keeps running).",
        "In a shell, llm-bridge --help lists every command.",
    ]
    return "\n".join(lines)


COMMANDS: dict[str, tuple[str, str]] = {
    "mission": ("Save and inspect durable missions", "mission list"),
    "mission create": ("Save an objective and its first task; no provider starts", "mission create --name demo --mode native --file prompt.md --cwd ."),
    "mission list": ("List saved missions; no state changes or provider work", "mission list --json"),
    "status": ("Inspect desired state, observed work and blockers", "status --mission ID"),
    "agents": ("Inspect current Bridge operations and provider-owned child agents; use --mission ID or --all-missions", "agents --all-missions --json"),
    "suspend-until-credits": ("Arm a wake timer and suspend after a fresh capacity and safety check", "suspend-until-credits --mission ID"),
    "task": ("Add, inspect, amend, approve and reject mission tasks", "task list --mission ID"),
    "task add": ("Save a new task for the specified mission", "task add --mission ID --file prompt.md"),
    "task list": ("List tasks in the specified mission", "task list --mission ID --json"),
    "task show": ("Read the complete task text, recorded evidence and any pending amendment", "task show --mission ID --task 1 --json"),
    "task output": ("Read the provider's output of the task's last run; pages it on a terminal, prints it when piped", "task output --mission ID --task 1"),
    "task diff": ("Show what the task's last run changed in the working tree since the checkpoint recorded at its start", "task diff --mission ID --task 1"),
    "task amend": ("Replace the task text: applies at once, or after the current step when a provider is working on the task", "task amend --mission ID --task 1 --file revised.md"),
    "task priority": ("Change task scheduling priority; default priority is zero", "task priority --mission ID --task 1 --value 5"),
    "task reorder": ("Change a task's ordering value", "task reorder --mission ID --task 1 --value 10"),
    "task dependencies": ("Require another task before this task can run", "task dependencies --mission ID --task 2 --add 1"),
    "task pause": ("Hold the task so that it does not start", "task pause --mission ID --task 1"),
    "task resume": ("Make a held task runnable again", "task resume --mission ID --task 1"),
    "task cancel": ("Cancel the task; its record is kept", "task cancel --mission ID --task 1"),
    "task retry": ("Queue another attempt after inspecting the failure", "task retry --mission ID --task 1"),
    "task approve": ("Approve a task in REVIEW as complete; --evidence records the reviewer's note first. Pairs with task reject", "task approve --mission ID --task 1 --evidence 'tests passed'"),
    "task verify": ("Record verification evidence for a task in REVIEW without approving it (to approve, use task approve)", "task verify --mission ID --task 1 --evidence 'tests passed'"),
    "task reject": ("Send a task in REVIEW back with a reason; it is queued for another attempt and the next prompt quotes the reason", "task reject --mission ID --task 1 --reason 'tests were not run'"),
    "rotation": ("Show or change the mission's saved provider rotation; a change applies from the next provider selection", "rotation --mission ID --set claude-sonnet,codex"),
    "proposal": ("Review task proposals before adding them to work", "proposal list --mission ID"),
    "proposal create": ("Record a proposal linked to an owned operation", "proposal create --mission ID --operation OP --file proposal.md"),
    "proposal list": ("List pending proposals by default", "proposal list --mission ID --status ALL"),
    "proposal accept": ("Accept a pending proposal into mission work", "proposal accept --mission ID --task 1"),
    "proposal reject": ("Reject a pending proposal with a reason", "proposal reject --mission ID --task 1 --reason 'out of scope'"),
    "events": ("Show the newest mission events in plain words, oldest first; controller bookkeeping is hidden unless --verbose. --since 10m limits the age, --follow keeps printing new events, --after ID reads forward from a global event ID", "events --mission ID --limit 20"),
    "start": ("Start working on the mission's tasks with the first provider that has capacity, in the mission's saved mode unless --mode says otherwise", "start --mission ID"),
    "pause": ("Hold the mission after the provider's current step; never suspends this computer", "pause --mission ID"),
    "resume": ("Allow the controller to continue eligible work", "resume --mission ID"),
    "continue": ("Start the next task now while a reviewed task stays in REVIEW (native missions hold for review by default)", "continue --mission ID"),
    "stop": ("Stop Bridge's controller for the mission; --force also ends a provider that does not exit by itself", "stop --mission ID"),
    "cancel": ("Cancel this mission's work; unrelated sessions are unaffected", "cancel --mission ID"),
    "attach": ("Open the mission's station (its provider terminal); starts no provider", "attach --mission ID"),
    "missions": ("List every mission, live ones first; --pick switches to one (in a station: Ctrl-] then M)", "missions --json"),
    "gc": ("Close the idle tmux stations of finished missions; a station with a live provider process or an attached terminal is kept", "gc --json"),
    "finish": ("Ask the native provider to exit (its own /exit or /quit) so the task moves to verification", "finish --mission ID"),
    "detach": ("Detach another terminal from the station, from outside; to leave your own terminal press Ctrl-] d (in the console: /leave)", "detach --mission ID"),
    "doctor": ("Inspect local prerequisites and persistence; exits 1 when no provider can run; mission is optional", "doctor --json"),
    "control": ("Inspect and steer the mission in Bridge controls; no autostart", "control --mission ID"),
    "usage": ("Inspect background capacity observations for this mission", "usage --mission ID"),
    "station": ("Select the viewed station; does not reroute a running task", "station --mission ID codex"),
    "steer": ("Change what happens at the next safe point: hand over to another provider, or add a review of a task", "steer --mission ID --kind handoff --to codex"),
    "help": ("Read offline workflow topics or a command help page", "help quickstart"),
    "view": ("Read a UTF-8 artifact; following is opt-in and starts no work", "view --file notes.md"),
    "mission set": ("Change a saved mission setting: automatic continue, team guidance, token spend, or suspending when rate limited", "mission set --mission ID --auto-continue on"),
    "mission colour": ("Pin the mission's identity colour (1-8) or return to the automatic one", "mission colour --mission ID 3"),
    "theme": ("Show the detected terminal background, the active colour theme and a swatch per role", "theme --theme light"),
    "backup": ("Write a consistent copy of the Bridge database (missions, tasks, events); an existing file is never overwritten", "backup --output bridge-backup.db"),
    "restore": ("Replace the Bridge database with a backup; the current one is kept beside it", "restore --input bridge-backup.db"),
    "remote": ("Configure this CLI to reach other computers through a coordinator, then use --target WORKER", "remote status"),
    "remote login": ("Store the coordinator URL, CA and API token", "remote login --coordinator https://bridge.home.arpa:8443 --token-file api.token"),
    "remote status": ("Show the configured coordinator without the token", "remote status"),
    "remote logout": ("Delete the stored coordinator URL, CA and API token", "remote logout"),
    "worker": ("Optional: let a Bridge coordinator use this computer; nothing connects unless you enrol and run it", "worker status"),
    "worker enroll": ("Store the coordinator URL, CA and worker token for this computer", "worker enroll --name workstation --coordinator wss://bridge.home.arpa:8443 --token-file worker.token"),
    "worker status": ("Show this computer's enrolment without the token", "worker status"),
    "worker run": ("Run the worker in the foreground until interrupted", "worker run"),
    "worker audit": ("Show this worker's security audit log (no tokens or terminal content)", "worker audit --limit 20"),
    "worker install-service": ("Install and start a systemd user unit that runs the worker", "worker install-service --print"),
    "worker uninstall-service": ("Stop and remove the worker's systemd user unit", "worker uninstall-service"),
    "worker unenroll": ("Delete this computer's enrolment, token and coordinator CA", "worker unenroll"),
    "worker list": ("List the workers registered with the configured coordinator (run it on the client, not on a worker)", "worker list"),
}


def _mission_create_defaults() -> str:
    """The ``mission create`` default-rotation line; read from the llm-tools config only when help is formatted."""
    from .bridge import resolved_default_rotation

    providers, routes = resolved_default_rotation()
    rotation = f"routes {routes}" if routes else f"providers {providers}"
    return f"\nDefaults: cwd is the current directory; {rotation} (from your llm-tools config when set);\nmode is Background unless --mode says otherwise."


_PAID = "starts provider work (uses paid capacity)"

# What a command changes, for commands that change anything. Read-only commands have no entry and
# therefore no "Effects:" line on their page.
EFFECTS: dict[str, str] = {
    "mission create": "saves a new mission in Bridge's database; starts no provider.",
    "mission set": "changes a saved setting of the mission.",
    "mission colour": "changes the mission's saved colour.",
    "task add": "adds a task to the mission; if the mission is running, the provider uses paid capacity on it when its turn comes.",
    "task amend": "replaces the task's text; a provider that is working on it sees the change after its current step.",
    "task priority": "changes the order in which tasks run.",
    "task reorder": "changes the order in which tasks run.",
    "task dependencies": "changes which tasks must finish before this one can run.",
    "task pause": "holds the task so that it does not start.",
    "task resume": "makes a held task runnable; a running mission " + _PAID + " on it.",
    "task cancel": "cancels the task and keeps its record.",
    "task retry": "queues another attempt; a running mission " + _PAID + ".",
    "task approve": "marks the task completed; a running mission then moves on to its next task.",
    "task verify": "records evidence on the task; starts no provider.",
    "task reject": "sends the task back with your reason; a running mission " + _PAID + " for the next attempt.",
    "proposal create": "records a proposal; it adds no work until it is accepted.",
    "proposal accept": "adds the proposed task to the mission; a running mission " + _PAID + " on it.",
    "proposal reject": "rejects the proposal; it adds no work.",
    "start": _PAID + ".",
    "pause": "holds the mission after the provider's current step; it does not suspend this computer.",
    "resume": _PAID + ".",
    "continue": _PAID + ".",
    "stop": "stops the mission's controller; with --force it also ends a running provider.",
    "cancel": "cancels the mission and its unfinished tasks, and ends their provider work.",
    "attach": "opens the station terminal; starts no provider.",
    "detach": "disconnects a station client; the mission keeps running.",
    "finish": "types the provider's own exit command into its terminal so the task moves to review.",
    "station": "changes which provider's terminal the controls show; it does not reroute running work.",
    "rotation": "with --set, changes the providers the mission uses from its next provider choice; without it, only shows them.",
    "steer": "queues a handoff or a review for the mission.",
    "suspend-until-credits": "suspends this computer until credits recover; a wake timer is armed first.",
    "gc": "closes the idle stations of finished missions.",
    "backup": "writes a copy of the Bridge database to --output.",
    "restore": "replaces the Bridge database with the backup; the current one is kept beside it.",
    "remote login": "stores the coordinator URL and API token on this computer.",
    "remote logout": "deletes the stored coordinator URL and API token.",
    "worker enroll": "stores the coordinator URL and worker token on this computer.",
    "worker run": "lets the coordinator reach this computer; with --allow-control at enrolment it can start and control missions here.",
    "worker install-service": "installs and starts a systemd user unit that runs the worker.",
    "worker uninstall-service": "stops and removes the worker's systemd user unit.",
    "worker unenroll": "deletes this computer's enrolment, token and coordinator CA.",
}

# Subcommand order on group pages, most common first; commands not named follow in registered order.
_COMMAND_ORDER: dict[str, tuple[str, ...]] = {
    "mission": ("create", "list", "set", "colour"),
    "task": ("add", "list", "show", "approve", "reject", "amend", "verify", "output", "diff", "pause", "resume", "cancel", "retry", "priority", "reorder", "dependencies"),
    "proposal": ("list", "accept", "reject", "create"),
}


def _ordered_names(path: str, names: list[str]) -> list[str]:
    if path:
        preferred: list[str] = list(_COMMAND_ORDER.get(path, ()))
    else:
        preferred = [name.split()[0] for _, entries in COMMAND_GROUPS for name, _ in entries]
    first = [name for name in dict.fromkeys(preferred) if name in names]
    return first + [name for name in names if name not in first]


def _row_text(text: str) -> str:
    """One listing row from a description: its first clause, with a lowercase first letter."""
    clause = text.replace(":", ";").split(";")[0].strip().rstrip(".")
    if len(clause) > 1 and not clause[1].isupper():
        clause = clause[0].lower() + clause[1:]
    return clause


def decorate_parser(parser: object) -> None:
    """Add shared public descriptions without changing the argument grammar."""
    import argparse
    assert isinstance(parser, argparse.ArgumentParser)
    internal = {"console", "_controller", "_station-gc"}
    class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        def _get_help_string(self, action: argparse.Action) -> str | None:
            # Show a default only when it carries information: never "None",
            # "False", an empty value, or text that already states its default.
            text = action.help or ""
            if "default" in text or action.dest == "mission" or action.default in (None, False, "", [], (), argparse.SUPPRESS):
                return text
            return super()._get_help_string(action)

        def _format_action(self, action: argparse.Action) -> str:
            # A command list prints one row per command, without the "{a,b,c}" line argparse puts above it.
            if isinstance(action, argparse._SubParsersAction):
                return "".join(self._format_action(item) for item in action._get_subactions())
            return super()._format_action(action)

    def visit(current: argparse.ArgumentParser, path: str) -> None:
        info = COMMANDS.get(path)
        current.formatter_class = HelpFormatter
        if info:
            description, example = info
            lines = [description.rstrip(".") + "."]
            effect = EFFECTS.get(path)
            if effect:
                lines.append("Effects: " + effect)
            lazy_defaults = _mission_create_defaults if path == "mission create" else None
            if path == "detach":
                lines += [
                    "Removes the station's one connected writable client, normally another terminal's.",
                    "With several attached (for example local and SSH), pass --client /dev/pts/N.",
                    "To leave your own terminal, press Ctrl-] then d (safe over SSH).",
                ]
            if path == "steer":
                lines.append(
                    "Other kinds (replace, verify, pause-after-iteration, stop-after-task) still work in older scripts. "
                    "To replace a task's text use task amend, to check a task use task verify, and to hold the mission use pause."
                )
            current.description = "\n".join(textwrap.fill(line, 78) for line in lines)
            if lazy_defaults is not None:
                base_description = current.description
                original_format_help = current.format_help

                def format_help() -> str:
                    current.description = base_description + lazy_defaults()
                    return original_format_help()

                current.format_help = format_help  # type: ignore[method-assign]
            current.epilog = "Example:\n  llm-bridge " + example.replace("--mission ID", "--mission 3f9a1c2b")
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                choices = action.choices
                helps = {item.dest: item.help for item in action._choices_actions}
                ordered = _ordered_names(path, list(choices))
                reordered = {name: choices[name] for name in ordered}
                choices.clear()
                choices.update(reordered)
                rows: list[argparse.Action] = []
                visited: list[object] = []
                for name, child in reordered.items():
                    if name in internal or child in visited:  # an alias shares its command's row
                        continue
                    visited.append(child)
                    child_path = (path + " " + name).strip()
                    own = COMMANDS.get(child_path)
                    text = _row_text(own[0]) if own else _row_text(str(helps.get(name) or name))
                    rows.append(action._ChoicesPseudoAction(name, [], text))
                    visit(child, child_path)
                action._choices_actions[:] = rows
                action.metavar = "{" + ",".join(row.dest for row in rows) + "}"
                if path:
                    group = current.add_argument_group("Commands")
                    current._positionals._group_actions.remove(action)
                    group._group_actions.append(action)
                    current._action_groups.remove(group)
                    current._action_groups.insert(current._action_groups.index(current._optionals), group)
            elif action.help is None:
                action.help = {
                    "mission": "target mission ID",
                    "task": "target task number",
                    "json": "emit the stable JSON response",
                    "prompt_file": "read complete source from PATH; '-' reads stdin",
                    "prompt": "literal source; use --file for large documents",
                    "headless": "run without a terminal station",
                    "mode": "how the provider runs",
                    "providers": "provider rotation order",
                    "routes": "configured launch/capacity route IDs",
                }.get(action.dest, action.dest.replace("_", " "))
            elif path == "help" and action.dest == "topic":
                action.help = f"a help topic ({topic_list_text()}) or a command such as: task verify"
        advanced_names = {"verifier_argv", "verifier_timeout", "log_max_bytes", "log_rotations", "log_retained_runs", "capacity_refresh_seconds", "command_template"}
        advanced_actions = [action for action in current._optionals._group_actions if action.dest in advanced_names]
        if advanced_actions:
            advanced = current.add_argument_group("Advanced verification and retention")
            for action in advanced_actions:
                current._optionals._group_actions.remove(action)
                advanced._group_actions.append(action)
    visit(parser, "")
    parser.description = "llm-bridge — supervise AI work in your terminal"
    parser.usage = "llm-bridge [--target WORKER] [COMMAND] [ARGS]"
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action.metavar = "COMMAND"
    # The root page is grouped text (COMMAND_GROUPS), not argparse's generated list; every command
    # still has its own --help.
    parser.format_help = root_help  # type: ignore[method-assign]
