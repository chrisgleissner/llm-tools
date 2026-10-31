"""Offline help shipped in the Python package and shared with the terminal UI."""
from __future__ import annotations

TOPICS: dict[str, str] = {
    "quickstart": """LLM Bridge — supervise AI work in your terminal

Needs: Python 3.11+, tmux, and one provider CLI that is installed and signed in.
Check with llm-bridge doctor; its Next line says what to do.

First mission, from your project folder. Opening Bridge starts no provider.

1. Run llm-bridge, then press N.
2. Type the task. The form selects the first provider that is installed and signed in.
3. Press Tab five times to reach "Start and open provider", then Enter. This uses
   paid capacity. Claude Code may first ask whether to trust the folder: choose
   "Yes, I trust this folder" (the first option is No, exit; if you pick it, Bridge holds
   the task until you run /start again).
4. When the provider says it is done, exit it with /exit. The station then says the
   task is waiting for your review.
5. Press Ctrl-] then B, type /approve, press Enter, then y. The mission shows COMPLETED.
6. Press Ctrl-] then d to leave the station, and Ctrl-D to leave the controls.

The same from a script, with no terminal attached (run inside the mission's folder,
or add --mission ID):

  llm-bridge mission create --file task.md --name demo --native
  llm-bridge start                        starts provider work; may consume paid capacity
  llm-bridge status                       after the provider exits, "Needs you" shows the approve command
  llm-bridge task verify --task 1 --approve --evidence 'tests passed'

A zero provider exit means awaiting verification, not completed work. Review the
evidence before approving. Reopen a station with llm-bridge attach. Add more work
with llm-bridge task add --file next-task.md. Ctrl-] B opens Bridge and Ctrl-] P
returns to the same provider process.

Three modes. Native (--native) keeps the provider's own interactive terminal in a
tmux station. Headless (--headless) runs the provider without a terminal, with
no station and no tmux. Background, the default when neither flag is given, runs
the provider without a terminal under a background controller and shows its
activity log in a tmux station. Use --native or --headless, not both. The home
form offers Native (recommended) and Headless.
CLI defaults and saved modes remain compatible. CLI creation rotates over the providers (or routes) set in your
llm-tools config ([ralph] wins over [defaults]); without such a config it uses
the built-in claude,codex,opencode. --providers or --routes override either.

Typing in the Bridge command line: bare commands and Codex/Claude-style slash
commands are the same: /start, /attach, /pause, /status and /approve [N] work
like their bare forms (N is the task to approve; it may be omitted when exactly
one task awaits approval). /help and ? show help.

Detach your own terminal with Ctrl-] then d, which works the same locally and
over SSH. Reattach with llm-bridge attach. The command llm-bridge detach is
different: it removes a station client from outside, normally another terminal's
(for example one still open at the machine while you are on SSH). Do not use it
to leave your own session. Detaching or exiting Bridge does not stop the mission.
Pause waits at the current invocation boundary and never suspends the host.
Stop and cancel are separate explicit operations.

More: help keys, remote, prompts, markdown, providers, states, verification, recovery.
Every public command also has --help. ID and task numbers are placeholders.
--mission accepts a unique ID prefix (4+ characters) and may be omitted when you
run the command inside the mission's folder.""",
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

Bridge body: F1-F8 select Overview, Tasks, Providers, Activity, Sessions, Health,
Actions, Usage. Line 1 is a live status strip (provider, task, inactivity,
capacity). Typing a view name (overview, providers, activity, sessions, health, actions,
usage) switches to it; the older names orders, stations, log, systems and palette still
work. Tab and Shift-Tab cycle views. Up/Down select; PgUp/PgDn page; Home/End select endpoints.
Enter opens the labelled selection. ? opens context help.
Overview o opens the complete objective; Tasks v opens the complete prompt. Printable text starts
a command; Ctrl-G focuses the retained draft; click the input to focus it.

Command input: Left/Right move; Home/End or Ctrl-A/Ctrl-E move to endpoints.
Backspace/Delete remove adjacent text. Ctrl-U clears. After a failed command the line stays empty; Up recalls it.
Ctrl-P/Ctrl-N recall history and restore the pre-history draft. Tab completes.
Enter submits an explicit command. ? is ordinary input. Escape returns to the
body with the draft retained. Ctrl-C cancels local editing first. Ctrl-D exits
only with empty input and no unresolved mutation; exit does not stop work.

Body Ctrl-E composes/adds a task; Tasks body Ctrl-R amends the selected task.
Ctrl-E in command input means End. Ctrl-K opens Actions from any view with the filter ready. Activity: Ctrl-T hides or shows controller bookkeeping rows. Body Ctrl-F opens Tasks search; Enter applies,
Escape cancels and restores the command draft. Clear the query to show all tasks.
Actions lists what applies to the current state, with the command for each; / filters them. Single-click selects; Enter/double-click
activates the selected action. Disabled actions explain their unavailable target.

Composer: Enter newline; Tab indent; PgUp/PgDn page; F2/Ctrl-S saves. Ctrl-O opens VISUAL/EDITOR.
Escape retains a recovery draft. Ctrl-D offers explicit discard confirmation.
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
    "console": """Bridge console: commands at the bottom line

Type at the bottom line. Slash commands work like Codex and Claude Code, and the
mission is implied. Bare words do the same (start, pause, ...).
  /start /resume /pause /stop    start, continue or hold provider work
  /attach  /detach               open the provider terminal; Ctrl-] P returns
  /approve [N]  /reject N WHY    decide on a task that awaits approval
  /status  /tasks  /proposals    print the current state
  /missions  /home               switch to another mission (the mission picker)
  /overview /tasks /providers /activity /health /actions /usage   open a view
  /help [topic]                  this page; topics: keys, prompts, providers, states
Keys: Ctrl-E add a task, Ctrl-R amend the selected task, Ctrl-K find an action,
Tab completes, ? explains the current view, Ctrl-D leaves (the mission keeps running).
Commands that run in a shell, such as llm-bridge task add --file F, are the same
commands without the slash. llm-bridge --help lists them all.""",
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
    "prompts": """Compose, import, amend and recover complete prompts

A 30 KiB or larger document belongs in the multiline composer or a file, not a
quoted command. In the home Objective field, open the composer with Ctrl-E.
Enter inserts a newline; F2/Ctrl-S saves back to the form without starting work.
Escape retains a draft; explicit discard is separate. Check byte/line count and
tail before saving. The interactive ceiling is 1 MiB of encoded UTF-8 source,
not a provider context limit. Oversize transactions must leave prior text intact.

Bracketed paste is a single transaction; no command/navigation occurs before its
terminator. Without bracketed paste a terminal cannot reliably distinguish paste
from typing: use the multiline composer, where Enter always inserts a newline.
Do not paste multiline documents into the short command input. Complex emoji
shaping depends on the terminal; common wide and combining characters are handled.

Import without the interactive ceiling:
  llm-bridge mission create --name demo --native --file prompt.md --cwd .
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
retrying or creating a new mission. Terminal missions cannot accept new tasks.
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

Native supervises a real CLI in a private owned tmux station. Background (no
flag) runs the provider without a terminal and shows its activity log in the
station; Headless (--headless) runs it with no station and no tmux.
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
Pause during an invocation means 'Pausing after current invocation': work still
runs until that boundary. Pause never suspends the host. ACTIVE requires a
currently executing owned operation; Last selected is history, not execution.
Completed missions have no active execution and cannot accept another task.

Provider zero exit enters verification. Passing required verification and operator
approval where configured establish completion. A native mission then holds
before its next task until you /approve or /reject the reviewed one, or run
/continue to start the next task while it keeps waiting (mission create
--auto-continue, or mission set --auto-continue on, turns the hold off). Blocked means a dependency,
verification request or uncertain operation needs attention: inspect status/events.
Stop ends the owned controller; --force is for deliberately terminating owned
work when graceful stop is insufficient. Cancel is a mission/task state mutation.
Neither operation should affect unrelated sessions. Exiting Bridge or detaching
only leaves the UI; it does not cancel or stop the controller.

Boundary steering example:
  llm-bridge steer --mission ID --kind pause-after-iteration
This queues intent for a safe boundary; acceptance is not proof it already applied.
Read submitting/accepted/deferred/applied/rejected feedback and current status.""",
    "verification": """Verify before declaring completion

A successful invocation is not successful work. Inspect task source, evidence and
acceptance conditions; use the actual mission and task IDs:
  llm-bridge task show --mission ID --task 1 --json
  llm-bridge task verify --mission ID --task 1 --evidence 'pytest: 12 passed'
  llm-bridge task verify --mission ID --task 1 --approve --evidence 'reviewed'
Approval marks work complete only through authoritative transition rules. UI
approval asks for target-aware confirmation; scripted CLI semantics are unchanged.

Configure a verifier when creating the mission; each repeated argument is one
argv item, not a shell command. Use equals for arguments beginning with '-':
  llm-bridge mission create --file prompt.md --verifier-arg python \
    --verifier-arg=-m --verifier-arg pytest --verifier-arg=-q
Default verifier timeout is 300 seconds. A verifier may use {context}, {cwd},
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
A task awaiting approval shows VERIFYING; approve it only after reading its evidence.""",
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
    "Usage": """Usage: the llm-usage dashboard for all providers

The table comes first. Keys: l show or hide the legend · Up/Down scroll
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


def topic_text(topic: str = "quickstart") -> str:
    key = " ".join(topic.strip().lower().split())
    if key in TOPICS:
        return TOPICS[key]
    parser = _command_path_parser(key)
    if parser is None:
        return f"Unknown help topic '{topic.strip() or '(empty)'}' · topics: {', '.join(TOPICS)} · or a command such as: task verify"
    return parser.format_help()


COMMANDS: dict[str, tuple[str, str]] = {
    "mission": ("Save and inspect durable missions", "mission list"),
    "mission create": ("Save an objective and its first task; no provider starts", "mission create --name demo --native --file prompt.md --cwd ."),
    "mission list": ("List saved missions; no state changes or provider work", "mission list --json"),
    "status": ("Inspect desired state, observed work and blockers", "status --mission ID"),
    "task": ("Add, inspect, amend and verify mission tasks", "task list --mission ID"),
    "task add": ("Save a new task for the specified mission", "task add --mission ID --file prompt.md"),
    "task list": ("List tasks in the specified mission", "task list --mission ID --json"),
    "task show": ("Read the complete task text, recorded evidence and any pending amendment", "task show --mission ID --task 1 --json"),
    "task amend": ("Replace the task text: applies at once, or after the current step when a provider is working on the task", "task amend --mission ID --task 1 --file revised.md"),
    "task priority": ("Change task scheduling priority; default priority is zero", "task priority --mission ID --task 1 --value 5"),
    "task reorder": ("Change a task's ordering value", "task reorder --mission ID --task 1 --value 10"),
    "task dependencies": ("Require another task before this task can run", "task dependencies --mission ID --task 2 --add 1"),
    "task pause": ("Hold the targeted task at a safe boundary", "task pause --mission ID --task 1"),
    "task resume": ("Make a held task eligible for the running controller", "task resume --mission ID --task 1"),
    "task cancel": ("Cancel the targeted task, preserving its durable record", "task cancel --mission ID --task 1"),
    "task retry": ("Queue another attempt after inspecting the failure", "task retry --mission ID --task 1"),
    "task verify": ("Record evidence with --evidence; add --approve to approve the task as complete", "task verify --mission ID --task 1 --approve --evidence 'tests passed'"),
    "task reject": ("Send a task awaiting verification back with a reason; it is queued for another attempt and the next prompt quotes the reason", "task reject --mission ID --task 1 --reason 'tests were not run'"),
    "rotation": ("Show or change the mission's saved provider rotation; a change applies from the next provider selection", "rotation --mission ID --set claude-sonnet,codex"),
    "proposal": ("Review task proposals before adding them to work", "proposal list --mission ID"),
    "proposal create": ("Record a proposal linked to an owned operation", "proposal create --mission ID --operation OP --file proposal.md"),
    "proposal list": ("List pending proposals by default", "proposal list --mission ID --status ALL"),
    "proposal accept": ("Accept a pending proposal into mission work", "proposal accept --mission ID --task 1"),
    "proposal reject": ("Reject a pending proposal with a reason", "proposal reject --mission ID --task 1 --reason 'out of scope'"),
    "events": ("Read bounded operational history; default limit is 100", "events --mission ID --limit 20 --json"),
    "start": ("Start the owned controller using saved mode unless overridden", "start --mission ID"),
    "pause": ("Hold after the current invocation; never suspend the host", "pause --mission ID"),
    "resume": ("Allow the controller to continue eligible work", "resume --mission ID"),
    "continue": ("Start the next task now while a reviewed task keeps awaiting approval (native missions hold for review by default)", "continue --mission ID"),
    "stop": ("Stop the owned controller; --force targets owned running work", "stop --mission ID"),
    "cancel": ("Cancel this mission's work; unrelated sessions are unaffected", "cancel --mission ID"),
    "attach": ("Open the existing owned station; starts no provider", "attach --mission ID"),
    "missions": ("List every mission, live ones first; --pick switches to one (in a station: Ctrl-] then M)", "missions --json"),
    "gc": ("Close the idle tmux stations of finished missions; a station with a live provider process or an attached terminal is kept", "gc --json"),
    "finish": ("Ask the native provider to exit (its own /exit or /quit) so the task moves to verification", "finish --mission ID"),
    "detach": ("Detach a station client from outside; your own terminal uses Ctrl-] d", "detach --mission ID"),
    "doctor": ("Inspect local prerequisites and persistence; mission is optional", "doctor --json"),
    "control": ("Inspect and steer the mission in Bridge controls; no autostart", "control --mission ID"),
    "usage": ("Inspect background capacity observations for this mission", "usage --mission ID"),
    "station": ("Select the viewed station; does not reroute a running task", "station --mission ID codex"),
    "steer": ("Queue typed intent for a task or safe execution boundary", "steer --mission ID --kind pause-after-iteration"),
    "help": ("Read offline workflow topics or a command help page", "help quickstart"),
    "view": ("Read a UTF-8 artifact; following is opt-in and starts no work", "view --file notes.md"),
}


def _mission_create_defaults() -> str:
    """The ``mission create`` default-rotation line; read from the llm-tools config only when help is formatted."""
    from .bridge import default_rotation

    providers, routes = default_rotation()
    rotation = f"routes {routes}" if routes else f"providers {providers}"
    return f"\nDefaults: cwd is the current directory; {rotation} (from your llm-tools config when set);\nscripted mode is Background unless --native/--headless is explicit."


def decorate_parser(parser: object) -> None:
    """Add shared public descriptions without changing the argument grammar."""
    import argparse
    assert isinstance(parser, argparse.ArgumentParser)
    internal = {"console", "_controller"}
    class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        def _get_help_string(self, action: argparse.Action) -> str | None:
            # Show a default only when it carries information: never "None",
            # "False", an empty value, or text that already states its default.
            text = action.help or ""
            if "default" in text or action.dest == "mission" or action.default in (None, False, "", [], (), argparse.SUPPRESS):
                return text
            return super()._get_help_string(action)

    def visit(current: argparse.ArgumentParser, path: str) -> None:
        info = COMMANDS.get(path)
        current.formatter_class = HelpFormatter
        if info:
            description, example = info
            starts = path in {"start", "resume", "task resume", "task retry", "task reject", "proposal accept"}
            effect = "Can enable provider work and consume paid capacity." if starts else "Does not directly start paid provider work."
            defaults = ""
            lazy_defaults = _mission_create_defaults if path == "mission create" else None
            if path == "detach":
                defaults = (
                    "\nRemoves the station's one connected writable client, normally another terminal's."
                    "\nWith several attached (for example local and SSH), pass --client /dev/pts/N."
                    "\nTo leave your own terminal, press Ctrl-] then d (safe over SSH)."
                )
            if path.startswith("task"):
                defaults += "\nUse the actual mission ID and task number; input files retain source text."
            current.description = description + ".\n" + effect + defaults
            if lazy_defaults is not None:
                base_description = current.description
                original_format_help = current.format_help

                def format_help() -> str:
                    current.description = base_description + lazy_defaults()
                    return original_format_help()

                current.format_help = format_help  # type: ignore[method-assign]
            shorthand = ""
            if "--mission ID" in example:
                shorthand = (
                    "\n  llm-bridge " + example.replace("--mission ID", "--mission 3f9a1c2b")
                    + "\n\n--mission takes a unique ID prefix (4+ characters) and may be omitted inside the mission's folder."
                )
            current.epilog = "Example:\n  llm-bridge " + example + shorthand + "\n\nMore: llm-bridge help keys · llm-bridge help recovery"
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                public = [name for name in action.choices if name not in internal]
                action.metavar = "{" + ",".join(public) + "}"
                action._choices_actions[:] = [item for item in action._choices_actions if item.dest not in internal]
                existing = {item.dest for item in action._choices_actions}
                for name, child in action.choices.items():
                    if name in internal:
                        continue
                    child_path = (path + " " + name).strip()
                    if name not in existing:
                        action._choices_actions.append(action._ChoicesPseudoAction(name, [], COMMANDS.get(child_path, (name, ""))[0]))
                    visit(child, child_path)
            elif action.help is None:
                action.help = {
                    "mission": "target mission ID",
                    "task": "target task number",
                    "json": "emit the stable JSON response",
                    "prompt_file": "read complete source from PATH; '-' reads stdin",
                    "prompt": "literal source; use --file for large documents",
                    "headless": "run without a terminal station",
                    "providers": "provider rotation order",
                    "routes": "configured launch/capacity route IDs",
                }.get(action.dest, action.dest.replace("_", " "))
        advanced_names = {"verifier_argv", "verifier_timeout", "log_max_bytes", "log_rotations", "log_retained_runs", "capacity_refresh_seconds", "command_template"}
        advanced_actions = [action for action in current._optionals._group_actions if action.dest in advanced_names]
        if advanced_actions:
            advanced = current.add_argument_group("Advanced verification and retention")
            for action in advanced_actions:
                current._optionals._group_actions.remove(action)
                advanced._group_actions.append(action)
    visit(parser, "")
    parser.description = "llm-bridge — supervise AI work in your terminal"
    parser.usage = "llm-bridge [COMMAND ...]"
    # Keep the root a short entry page; every command still has nested --help.
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action._choices_actions.clear()
            action.metavar = "COMMAND"
    parser.epilog = """Start here (opening starts no provider)
  llm-bridge                       Open missions
  llm-bridge help quickstart        Learn the basic workflow
  llm-bridge help prompts           Compose/import large prompts

Common commands
  mission create   Save a mission and first task
  start / attach   Start work / reopen its existing station
  status / task    Inspect work; add, amend and verify tasks
  pause / stop     Hold at an invocation boundary / stop controller

Also: missions, finish, mission list, mission colour, rotation, theme, resume, cancel,
detach, proposal, events, doctor, control, usage, station, steer, view, help.
More: llm-bridge COMMAND --help · llm-bridge help keys"""
