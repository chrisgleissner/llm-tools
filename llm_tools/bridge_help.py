"""Offline help shipped in the Python package and shared with the terminal UI."""
from __future__ import annotations

TOPICS: dict[str, str] = {
    "quickstart": """LLM Bridge — supervise AI work in your terminal

Opening Bridge or a saved mission starts no provider. Save first; start explicitly.

1. Save: llm-bridge mission create --name demo --native --file prompt.md --cwd .
   This saves the objective and first task. Note the returned mission ID.
2. Start: llm-bridge start --mission ID
   This starts provider work and may consume paid capacity. Saved mode is reused.
3. Open: llm-bridge attach --mission ID
   Ctrl-] B opens Bridge; Ctrl-] P returns to the same provider process.
4. Inspect: llm-bridge status --mission ID
   A zero provider exit means awaiting verification, not completed work.
5. Verify: llm-bridge task verify --mission ID --task 1 --evidence 'tests passed'
   Use the actual task ID. Review evidence before approving work as complete.
   Add more work: llm-bridge task add --mission ID --file next-task.md

Native interactive keeps the complete provider interface as your workspace.
Autonomous runs headless provider work with activity output. Headless has no
station. The home form recommends Native; CLI defaults and saved modes remain
compatible. CLI creation defaults to claude,codex,opencode unless specified.

Detach with Ctrl-] d; reattach with the command above. Detaching or exiting Bridge
does not stop the mission. Pause waits at the current invocation boundary and
never suspends the host. Stop and cancel are separate explicit operations.

More: help keys, prompts, markdown, providers, states, verification, recovery.
Every public command also has --help. ID and task numbers are placeholders.""",
    "keys": """Keys and terminal ownership

Provider focus: ordinary keys, slash commands, arrows, Tab, Escape, Ctrl-C,
Ctrl-D, paste and requested mouse/focus events belong to the provider.
Ctrl-] is Bridge's prefix: b/B opens controls, p/P returns to Provider, ? shows
station keys, z toggles zoom, d detaches. Ctrl-] Ctrl-] sends a literal prefix.
Switching panes returns to the same process; it does not launch another provider.

Bridge body: F1-F8 select Bridge, Orders, Stations, Systems, Log, Sessions,
Palette, Usage. Orders means Tasks; Stations means Providers/routes. Tab and
Shift-Tab cycle views. Up/Down select; PgUp/PgDn page; Home/End select endpoints.
Enter opens the labelled selection. ? opens context help.
Bridge body o opens the complete objective; Orders v opens the complete prompt. Printable text starts
a command; Ctrl-G focuses the retained draft; click the input to focus it.

Command input: Left/Right move; Home/End or Ctrl-A/Ctrl-E move to endpoints.
Backspace/Delete remove adjacent text. Ctrl-U clears; Ctrl-K deletes to end.
Ctrl-P/Ctrl-N recall history and restore the pre-history draft. Tab completes.
Enter submits an explicit command. ? is ordinary input. Escape returns to the
body with the draft retained. Ctrl-C cancels local editing first. Ctrl-D exits
only with empty input and no unresolved mutation; exit does not stop work.

Body Ctrl-E composes/adds a task; Orders body Ctrl-R amends the selected task.
Ctrl-E in command input means End. Body Ctrl-F opens Orders search; Enter applies,
Escape cancels and restores the command draft. Clear the query to show all tasks.
Palette / filters labelled actions. Single-click selects; Enter/double-click
activates the selected action. Disabled actions explain their unavailable target.

Composer: Enter inserts a newline; F2/Ctrl-S saves; Ctrl-O opens VISUAL/EDITOR.
Escape retains a recovery draft. Ctrl-D offers explicit discard confirmation.
Viewer: Up/Down, PgUp/PgDn or Space page; Home/End go first/last; / searches,
n/N visit matches; Escape cancels search before closing, q closes the viewer.
m toggles source/render, w toggles wrap, Left/Right scroll unwrapped lines.
h lists headings; s exports source to a new path; e edits an eligible task;
c copies source when a supported local helper is available.

Native application scrolling and tmux scrollback differ. With mouse capture,
Shift-drag selection depends on the terminal. Ctrl-] [ enters tmux copy mode;
q leaves copy mode. Use terminal paste shortcuts. No cross-platform clipboard
support or automatic OSC52 clipboard transmission is promised.""",
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

In Orders, open the selected full prompt with v. Bounded status previews are not
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
is the reliable fallback; Bridge never silently sends clipboard escape sequences.
Retained provider logs are bounded; pruned content cannot be recovered by paging.
Open an artifact with llm-bridge view --file notes.md. Add --follow only to
follow changes: scrolling up pauses, End resumes, and rotation/truncation is
labelled. Without a TTY, view prints complete source; --follow requires a TTY.""",
    "providers": """Providers, routes and native stations

Native interactive supervises a real CLI in a private owned tmux station.
Autonomous runs headless work with activity; --headless has no station.
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
approval where configured establish completion. Blocked means a dependency,
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


def topic_text(topic: str = "quickstart") -> str:
    key = " ".join(topic.strip().lower().split())
    if key in TOPICS:
        return TOPICS[key]
    # Import the grammar only when command-path help is requested. No database,
    # renderer or terminal service is needed to format argparse help.
    from .bridge import _build_parser
    import argparse
    parser = _build_parser()
    for word in key.split():
        groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if not groups or word not in groups[0].choices or word in {"console", "_controller"}:
            return f"Unknown help topic: {topic or '(empty)'}\nTopics: {', '.join(TOPICS)}\nTry llm-bridge help quickstart."
        parser = groups[0].choices[word]
    return parser.format_help()


COMMANDS: dict[str, tuple[str, str]] = {
    "mission": ("Save and inspect durable missions", "mission list"),
    "mission create": ("Save an objective and its first task; no provider starts", "mission create --name demo --native --file prompt.md --cwd ."),
    "mission list": ("List saved missions; no state changes or provider work", "mission list --json"),
    "status": ("Inspect desired state, observed work and blockers", "status --mission ID"),
    "task": ("Add, inspect, amend and verify mission tasks", "task list --mission ID"),
    "task add": ("Save a new task for the specified mission", "task add --mission ID --file prompt.md"),
    "task list": ("List tasks in the specified mission", "task list --mission ID --json"),
    "task show": ("Read the complete task source and evidence", "task show --mission ID --task 1 --json"),
    "task amend": ("Queue complete replacement source at a safe boundary", "task amend --mission ID --task 1 --file revised.md"),
    "task priority": ("Change task scheduling priority; default priority is zero", "task priority --mission ID --task 1 --value 5"),
    "task reorder": ("Change a task's ordering value", "task reorder --mission ID --task 1 --value 10"),
    "task dependencies": ("Require another task before this task can run", "task dependencies --mission ID --task 2 --add 1"),
    "task pause": ("Hold the targeted task at a safe boundary", "task pause --mission ID --task 1"),
    "task resume": ("Make a held task eligible for the running controller", "task resume --mission ID --task 1"),
    "task cancel": ("Cancel the targeted task, preserving its durable record", "task cancel --mission ID --task 1"),
    "task retry": ("Queue another attempt after inspecting the failure", "task retry --mission ID --task 1"),
    "task verify": ("Record evidence; --approve explicitly approves completion", "task verify --mission ID --task 1 --evidence 'tests passed'"),
    "proposal": ("Review task proposals before adding them to work", "proposal list --mission ID"),
    "proposal create": ("Record a proposal linked to an owned operation", "proposal create --mission ID --operation OP --file proposal.md"),
    "proposal list": ("List pending proposals by default", "proposal list --mission ID --status ALL"),
    "proposal accept": ("Accept a pending proposal into mission work", "proposal accept --mission ID --task 1"),
    "proposal reject": ("Reject a pending proposal with a reason", "proposal reject --mission ID --task 1 --reason 'out of scope'"),
    "events": ("Read bounded operational history; default limit is 100", "events --mission ID --limit 20 --json"),
    "start": ("Start the owned controller using saved mode unless overridden", "start --mission ID"),
    "pause": ("Hold after the current invocation; never suspend the host", "pause --mission ID"),
    "resume": ("Allow the controller to continue eligible work", "resume --mission ID"),
    "stop": ("Stop the owned controller; --force targets owned running work", "stop --mission ID"),
    "cancel": ("Cancel this mission's work; unrelated sessions are unaffected", "cancel --mission ID"),
    "attach": ("Open the existing owned station; starts no provider", "attach --mission ID"),
    "detach": ("Detach a terminal client while mission work continues", "detach --mission ID"),
    "doctor": ("Inspect local prerequisites and persistence; mission is optional", "doctor --json"),
    "control": ("Inspect and steer the mission in Bridge controls; no autostart", "control --mission ID"),
    "usage": ("Inspect background capacity observations for this mission", "usage --mission ID"),
    "station": ("Select the viewed station; does not reroute a running task", "station --mission ID codex"),
    "steer": ("Queue typed intent for a task or safe execution boundary", "steer --mission ID --kind pause-after-iteration"),
    "help": ("Read offline workflow topics or a command help page", "help quickstart"),
    "view": ("Read a UTF-8 artifact; following is opt-in and starts no work", "view --file notes.md"),
}


def decorate_parser(parser: object) -> None:
    """Add shared public descriptions without changing the argument grammar."""
    import argparse
    assert isinstance(parser, argparse.ArgumentParser)
    internal = {"console", "_controller"}
    class HelpFormatter(argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter):
        pass

    def visit(current: argparse.ArgumentParser, path: str) -> None:
        info = COMMANDS.get(path)
        current.formatter_class = HelpFormatter
        if info:
            description, example = info
            starts = path in {"start", "resume", "task resume", "task retry", "proposal accept"}
            effect = "Can enable provider work and consume paid capacity." if starts else "Does not directly start paid provider work."
            defaults = ""
            if path == "mission create":
                defaults = "\nDefaults: cwd is the current directory; providers claude,codex,opencode;\nscripted mode is autonomous unless --native/--headless is explicit."
            if path.startswith("task"):
                defaults += "\nUse the actual mission ID and task number; input files retain source text."
            current.description = description + ".\n" + effect + defaults
            current.epilog = "Example:\n  llm-bridge " + example + "\n\nMore: llm-bridge help keys · llm-bridge help recovery"
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

Also: mission list, resume, cancel, detach, proposal, events, doctor,
control, usage, station, steer, view, help.
More: llm-bridge COMMAND --help · llm-bridge help keys"""
