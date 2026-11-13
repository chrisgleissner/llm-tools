"""Command-line parser construction for the Bridge CLI and console."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable, NoReturn, Sequence
from .bridge_states import MISSION_COUNT, MISSION_HUE_NAMES, THEME_CHOICES
from .bridge_missions import MIN_MISSION_PREFIX
import argparse
from .bridge import (
    EXIT_USAGE,
    MISSION_LIST_PAGE,
    RUN_MODES,
    TASK_LIST_PAGE,
    _CONSOLE_ONLY_COMMANDS,
    _add_task_selector,
    _db_int,
    _event_age,
    _fail,
    _positional_into,
    _read_prompt_args,
    resolved_default_rotation,
)


def _close_names(word: str, names: Sequence[str], *, best_only: bool = False) -> list[str]:
    """Up to three names that look like what was typed: close spellings first, then names that start with it.

    With ``best_only`` a close spelling is returned alone (the command line suggests one fix, not a list).
    """
    import difflib

    close = difflib.get_close_matches(word, list(names), n=1 if best_only else 3, cutoff=0.6 if best_only else 0.5)
    if best_only and close:
        return close
    if word:
        close += [name for name in names if name.startswith(word) and name not in close]
    return close[:3]


def _cli_parser_class() -> type[argparse.ArgumentParser]:
    """The command line's parser class: an unknown command or option gets a one-line suggestion.

    Created on first use so ``--version`` and similar never import argparse. Other argument errors
    keep argparse's own usage message.
    """
    existing = globals().get("_CliParser")
    if existing is not None:
        return existing
    import argparse

    class _CliParser(argparse.ArgumentParser):
        _arguments: list[str] = []

        def parse_args(self, args: Sequence[str] | None = None, namespace: argparse.Namespace | None = None) -> argparse.Namespace:  # type: ignore[override]
            # Shared through the class: a sub-command's parser reports its own errors and needs the whole command line.
            _CliParser._arguments = list(sys.argv[1:] if args is None else args)
            return super().parse_args(args, namespace)

        def _command_for_arguments(self) -> argparse.ArgumentParser:
            current: argparse.ArgumentParser = self
            for word in self._arguments:
                for action in current._actions:
                    if isinstance(action, argparse._SubParsersAction) and word in action.choices:
                        current = action.choices[word]
                        break
            return current

        def _usage_exit(self, text: str) -> NoReturn:
            """Exit 2 with ``llm-bridge: TEXT``; with ``--json`` print an error object on stdout instead."""
            _fail(text, "usage", EXIT_USAGE, bool({"--json", "-j"} & set(self._arguments)))
            raise SystemExit(EXIT_USAGE)

        def _target_placement_message(self) -> str:
            """``--target`` after the command: say where it goes, with the command line rewritten."""
            words = list(self._arguments)
            name = "NAME"
            for index, word in enumerate(words):
                if word == "--target" and index + 1 < len(words):
                    name = words[index + 1]
                    del words[index:index + 2]
                    break
                if word.startswith("--target="):
                    name = word.split("=", 1)[1]
                    del words[index]
                    break
            return f"--target goes before the command · llm-bridge --target {name} {' '.join(words)}".rstrip()

        def error(self, message: str) -> NoReturn:
            for action in self._actions:
                if isinstance(action, argparse._SubParsersAction):
                    unknown = re.match(rf"argument {re.escape(argparse._get_action_name(action) or '')}: invalid choice: '([^']*)'", message)
                    if unknown:
                        names = [name for name in action.choices if not name.startswith("_") and name != "console"]
                        kind = "command" if " " not in self.prog else f"{self.prog.split()[-1]} command"
                        hint = _close_names(unknown.group(1), names, best_only=True)
                        suggestion = f" · did you mean {', '.join(hint)}?" if hint else ""
                        self._usage_exit(f"unknown {kind} '{unknown.group(1)}'{suggestion} · {self.prog} --help lists commands")
            if message.startswith("unrecognized arguments: "):
                command = self._command_for_arguments()
                words = message[len("unrecognized arguments: "):].split(" ")
                options = sorted(
                    option for option, action in command._option_string_actions.items()
                    if option.startswith("--") and action.help != argparse.SUPPRESS
                )
                wrong = next((word.split("=", 1)[0] for word in words if word.startswith("--")), None)
                if wrong == "--target":
                    self._usage_exit(self._target_placement_message())
                if wrong is not None:
                    hint = _close_names(wrong, options, best_only=True)
                    suggestion = f" · did you mean {', '.join(hint)}?" if hint else ""
                    self._usage_exit(f"unknown option '{wrong}'{suggestion} · {command.prog} --help lists options")
                self._usage_exit(f"unrecognized arguments: {' '.join(words)} · {command.prog} --help shows the arguments")
            if {"--json", "-j"} & set(self._arguments):
                self._usage_exit(message)
            from .bridge_help import troubleshooting_reference

            # argparse's own report (usage, then "PROG: error: MESSAGE"), with the troubleshooting address added.
            self.print_usage(sys.stderr)
            self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\nsee: {troubleshooting_reference('usage')}\n")

    globals()["_CliParser"] = _CliParser
    return _CliParser


def _console_parser_class() -> type[argparse.ArgumentParser]:
    """The console's parser class, created on first use so ``--version`` and similar never import argparse."""
    existing = globals().get("_ConsoleParser")
    if existing is not None:
        return existing
    import argparse

    class _ConsoleParser(argparse.ArgumentParser):
        def error(self, message: str) -> NoReturn:
            unknown = re.search(r"invalid choice: '([^']*)'", message)
            if unknown:
                word = unknown.group(1)
                top_level = " " not in self.prog
                names = [
                    name
                    for action in self._actions
                    if isinstance(action, argparse._SubParsersAction)
                    for name in action.choices
                    if not name.startswith("_") and name != "console"
                ]
                if top_level:
                    names.extend(name for name in _CONSOLE_ONLY_COMMANDS if name not in names)
                close = _close_names(word, names)
                kind = "command" if top_level else f"{self.prog.split()[-1]} command"
                hint = f" · did you mean {', '.join(close[:3])}?" if close else ""
                raise ValueError(f"Unknown {kind} '{word}'{hint} · /help lists commands")
            raise ValueError(f"{message}. Use {self.prog} --help for the required arguments.")

    globals()["_ConsoleParser"] = _ConsoleParser
    return _ConsoleParser


def _add_mission_arg(parser: argparse.ArgumentParser, default_mission: str | None, *, required: bool = False, inferred: bool = True) -> None:
    help_text = f"mission ID or a unique prefix of {MIN_MISSION_PREFIX}+ characters"
    if inferred:
        help_text += "; omit it inside a mission's folder to use that mission"
    parser.add_argument("--mission", "-m", metavar="ID", required=required and default_mission is None, default=default_mission, help=help_text)


def _add_json_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", "-j", action="store_true", help="print the stable machine-readable JSON response instead of text; an error is then {\"error\": {\"code\", \"message\", \"hint\", \"help\"}} on standard output")


class _RotationDefaultText:
    """Help-text fragment naming the configured default rotation, read only when help is formatted.

    Reading the config file at parser construction would load the config and provider modules for every
    command; argparse converts a ``%(name)s`` value with ``str()`` at ``--help`` time instead.
    """

    def __init__(self, render: Callable[[str, str], str]) -> None:
        self._render = render

    def __str__(self) -> str:
        return self._render(*resolved_default_rotation())


def _add_mission_create_args(create: argparse.ArgumentParser, *, legacy: bool = False) -> None:
    """Options shared by ``mission create`` and the legacy bare ``--prompt`` form."""
    import argparse

    create.add_argument("--name", metavar="NAME", help="mission name shown in lists (default: the first line of the prompt, cut at a word boundary under 40 characters)")
    create.add_argument("--cwd", metavar="DIR", default=str(Path.cwd()), help="folder the providers work in (default: the current folder)")
    # argparse validates a help string when the option is added, so the lazy placeholders are set afterwards.
    providers_help = "comma-separated providers to rotate through, in order; an explicit list without --routes disables configured routes"
    providers_action = create.add_argument("--providers", metavar="LIST", default=None, help=providers_help)
    providers_action.help = providers_help + "%(rotation_providers)s"
    providers_action.rotation_providers = _RotationDefaultText(lambda providers, routes: "" if routes else f" (default: {providers})")  # type: ignore[attr-defined]
    routes_help = "comma-separated route IDs from the llm-tools config to rotate through instead of providers; wins over --providers"
    routes_action = create.add_argument("--routes", metavar="LIST", default=None, help=routes_help)
    routes_action.help = routes_help + "%(rotation_routes)s"
    routes_action.rotation_routes = _RotationDefaultText(lambda providers, routes: f" (default: {routes})" if routes else "")  # type: ignore[attr-defined]
    create.add_argument("--acceptance", metavar="TEXT", action="append", default=[], help="acceptance criterion for the first task; repeat for several")
    create.add_argument("--teams", choices=("on", "off"), default=None, help="provider-native team guidance (default: on; global default: [bridge].teams)")
    create.add_argument("--token-spend", choices=("low", "medium", "high"), default=None, help="token spend preference (default: medium; global default: [bridge].token_spend)")
    create.add_argument("--mode", choices=RUN_MODES, default=None, help=(
        "how the provider runs. Native: the provider's own interactive terminal runs in a station (open it with attach). "
        "Background: the provider runs under a controller and a station shows its activity log. "
        "Headless: no terminal and no station, so nothing to attach to. "
        "Background is the default because it needs no terminal, so scripts behave the same on every machine; "
        "the home screen offers all three and recommends Native"))
    # Older spellings of --mode native / --mode headless; still accepted, no longer advertised.
    create.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    create.add_argument("--native", dest="native_tui", action="store_true", help=argparse.SUPPRESS)
    create.add_argument("--verifier", dest="verifier_command", metavar="COMMAND", default=None, help="command that checks a finished task, split into words like a shell would (not run through a shell), for example 'python -m pytest -q'; or write the command last after --, as in -- python -m pytest -q; supports {context}, {cwd}, {mission_id}, {task_id}, {revision}")
    create.add_argument("--verifier-name", dest="verifier_name", metavar="NAME", default=None, help="a verifier declared as [bridge.verifiers.NAME] in the llm-tools config of the machine that runs the mission (the only verifier form a remote request may use)")
    create.add_argument("--verifier-arg", dest="verifier_argv", metavar="ARG", action="append", default=[], help="one argument of the verifier command, for exact control of quoting; repeat per argument (--verifier-arg=-m for one that starts with a dash)")
    create.add_argument("--verifier-file", dest="verifier_files", metavar="PATH", action="append", default=[], help="a file the verifier depends on, relative to --cwd; repeat per file; if it changes after the mission is created a passing verifier no longer completes a task without your approval (the verifier program and the script it interprets are tracked automatically)")
    create.add_argument("--verifier-timeout", metavar="SECONDS", type=float, default=None, help="seconds before a verifier run is treated as failed (default: 300); a --verifier-name uses its declared timeout unless this is given")
    create.add_argument("--log-max-bytes", metavar="BYTES", type=_db_int, default=4 * 1024 * 1024, help="size at which one provider log file rotates (default: 4194304, 4 MiB)")
    create.add_argument("--log-rotations", metavar="COUNT", type=_db_int, default=3, help="rotated log files kept per provider run (default: 3)")
    create.add_argument("--log-retained-runs", metavar="COUNT", type=_db_int, default=20, help="newest provider-run log directories kept; older ones are pruned (default: 20)")
    create.add_argument("--capacity-refresh-seconds", metavar="SECONDS", type=_db_int, default=60, help="seconds between background provider-capacity observations (default: 60)")
    create.add_argument("--auto-continue", action="store_true", help="native missions: start the next task as soon as a provider run ends, instead of holding until you approve or reject the reviewed task (background and headless missions never hold)")
    create.add_argument("--trust-folder", action="store_true", help="answer the provider's workspace-trust dialog for the mission folder automatically in Headless and Background modes; without it a run that stops at that dialog is aborted (trust the folder in the provider CLI once instead)")
    create.add_argument("--new", action="store_true", help="create the mission without the warning that this folder already has an unfinished mission")
    create.add_argument("--command-template", metavar="ARGV", help="launch command template for tests or custom providers, in Ralph/scheduler syntax")
    create.add_argument("--include-config", action="store_true", help="with --json, include the mission's saved copy of the llm-tools config (large; left out by default)")
    if legacy:
        create.add_argument("--no-start", action="store_true", help="save the mission without starting it")


class _SkippedParser:
    """Stand-in for a command's parser when only another command is being parsed; every call is ignored."""

    def __getattr__(self, name: str) -> "_SkippedParser":
        return self

    def __call__(self, *args: Any, **kwargs: Any) -> "_SkippedParser":
        return self


class _OnlyCommand:
    """The top-level sub-parser group, building the parser of one command and skipping the rest.

    Building all fifty command parsers costs more than running a short command, so the
    normal path builds just the requested one (see :func:`_parse_arguments`).
    """

    def __init__(self, group: Any, command: str) -> None:
        self._group = group
        self._command = command

    def add_parser(self, name: str, **kwargs: Any) -> Any:
        if name == self._command or self._command in kwargs.get("aliases", ()):
            return self._group.add_parser(name, **kwargs)
        return _SkippedParser()


def _disable_abbreviations(parser: argparse.ArgumentParser) -> None:
    """No parser accepts a prefix of a long option: ``stop --f`` is not ``stop --force`` (CLIUX-071).

    Explicit aliases (``--prompt-file``, ``-f``, ``-m``) are separate option strings and keep working.
    """
    import argparse

    parser.allow_abbrev = False
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _disable_abbreviations(child)


def _build_parser(
    *,
    parser_class: type[argparse.ArgumentParser] | None = None,
    default_mission: str | None = None,
    only: str | None = None,
) -> argparse.ArgumentParser:
    """The full command grammar, or with ``only`` just that command's (for parsing, not for help)."""
    import argparse

    if parser_class is None:
        parser_class = _cli_parser_class()
    parser = parser_class(prog="llm-bridge", description="Terminal command bridge for persistent LLM engineering missions")
    sub: Any = parser.add_subparsers(dest="command", required=True)
    if only is not None:
        sub = _OnlyCommand(sub, only)
    help_parser = sub.add_parser("help", help="show offline Bridge workflow and recovery guidance")
    help_parser.add_argument("topic", nargs="*", help="a help topic or a command such as: task verify")
    view = sub.add_parser("view", help="read a local text artifact without starting work")
    view_source = view.add_mutually_exclusive_group(required=True)
    view_source.add_argument("--file", metavar="PATH", help="UTF-8 text artifact to open; the source stays unchanged")
    view_source.add_argument("path", nargs="?", metavar="PATH", action=_positional_into("file"), help="the same as --file PATH")
    view.add_argument("--follow", action="store_true", help="follow changes explicitly; Up pauses, End resumes")

    mission = sub.add_parser("mission", help="create or list missions")
    mission_sub = mission.add_subparsers(dest="mission_action", required=True)
    create = mission_sub.add_parser("create", help="create a mission and its first task")
    _read_prompt_args(create, "mission objective and first task")
    _add_mission_create_args(create)
    _add_json_arg(create)
    listing = mission_sub.add_parser("list", help="list every mission (the same listing as llm-bridge missions)")
    listing.add_argument("--limit", metavar="N", type=_db_int, default=None, help=f"list at most N missions, live ones first (default: {MISSION_LIST_PAGE}; all with --json)")
    listing.add_argument("--include-config", action="store_true", help="with --json, include each mission's saved copy of the llm-tools config (large; left out by default)")
    _add_json_arg(listing)
    mission_delete = mission_sub.add_parser(
        "delete",
        help="permanently delete a finished or never-started mission and its local state",
        description="Delete a mission that never started or has ended or been stopped: its tasks, events, operations, logs and verifier files, "
                    "and (with a worker connected to a coordinator) the coordinator's copy. A running, paused or stopping mission is refused (exit status 4). "
                    "This cannot be undone. The mission must be named; the current folder is not used.")
    mission_delete.add_argument("--mission", "-m", metavar="ID", default=None, help=f"mission ID or a unique prefix of {MIN_MISSION_PREFIX}+ characters (required; never inferred from the folder)")
    mission_delete.add_argument("mission_ref", nargs="?", metavar="ID", action=_positional_into("mission"), help="the same as --mission ID")
    mission_delete.add_argument("--yes", action="store_true", help="delete without asking; required when there is no terminal to ask on (scripts). Deleting cannot be undone")
    _add_json_arg(mission_delete)
    mission_set = mission_sub.add_parser("set", help="change a mission setting")
    _add_mission_arg(mission_set, default_mission)
    mission_set.add_argument("--auto-continue", dest="auto_continue", choices=("on", "off"), help="on: start the next task without waiting for review of the previous one; off: hold until you approve, reject or continue (the default for native missions)")
    mission_set.add_argument("--teams", choices=("on", "off"), help="enable or disable provider-native team guidance")
    mission_set.add_argument("--token-spend", choices=("low", "medium", "high"), help="set token spend preference")
    mission_set.add_argument("--suspend-when-rate-limited", choices=("on", "off"), help="on: suspend at an idle capacity wait when no Bridge operation is active; off: wait awake (default)")
    _add_json_arg(mission_set)
    mission_settings = mission_sub.add_parser("settings", help="show a mission's saved execution settings")
    _add_mission_arg(mission_settings, default_mission)
    _add_json_arg(mission_settings)
    colour = mission_sub.add_parser("colour", aliases=["color"], help="pin the mission's identity colour (1-8) or return to the automatic one")
    _add_mission_arg(colour, default_mission)
    colour.add_argument("value", metavar="N|auto", help=f"colour number 1-{MISSION_COUNT} ({', '.join(f'{n} {name}' for n, name in enumerate(MISSION_HUE_NAMES, 1))}) or auto")
    _add_json_arg(colour)

    status = sub.add_parser("status")
    _add_mission_arg(status, default_mission)
    _add_json_arg(status)

    screen = sub.add_parser("screen", help="read a bounded snapshot of a native provider's current screen")
    _add_mission_arg(screen, default_mission)
    _add_json_arg(screen)

    agents = sub.add_parser("agents", help="inspect live Bridge provider operations and native Codex child agents")
    agent_scope = agents.add_mutually_exclusive_group()
    agent_scope.add_argument("--mission", dest="agent_mission", help="limit the map to one Bridge mission")
    agent_scope.add_argument("--all-missions", dest="agent_all_missions", action="store_true", help="include operations from every Bridge mission (default)")
    _add_json_arg(agents)

    suspend = sub.add_parser("suspend-until-credits", help="suspend at the earliest verified credit recovery with a wake timer armed")
    _add_mission_arg(suspend, default_mission)
    _add_json_arg(suspend)

    tasks = sub.add_parser("task")
    task_sub = tasks.add_subparsers(dest="task_action", required=True)
    add = task_sub.add_parser("add")
    _add_mission_arg(add, default_mission)
    _read_prompt_args(add)
    add.add_argument("--title", metavar="TEXT", default="", help="short task title shown in lists (default: first line of the prompt)")
    add.add_argument("--priority", metavar="N", type=_db_int, default=0, help="scheduling priority; a HIGHER number runs first (default: 0)")
    add.add_argument("--order", metavar="N", type=_db_int, help="ordering value among equal priorities; lower runs first (default: appended last)")
    add.add_argument("--depends-on", metavar="IDS", default="", help="comma-separated task numbers that must complete first")
    add.add_argument("--acceptance", metavar="TEXT", action="append", default=[], help="acceptance criterion for the task; repeat for several")
    _add_json_arg(add)
    task_list = task_sub.add_parser("list")
    _add_mission_arg(task_list, default_mission)
    task_list.add_argument("--all", action="store_true", help=f"list every task, however many (default: the first {TASK_LIST_PAGE})")
    task_list.add_argument("--limit", metavar="N", type=_db_int, default=TASK_LIST_PAGE, help=f"list at most N tasks (default: {TASK_LIST_PAGE})")
    task_list.add_argument("--offset", metavar="N", type=_db_int, default=0, help="skip the first N tasks of the list")
    _add_json_arg(task_list)
    show = task_sub.add_parser("show")
    _add_mission_arg(show, default_mission)
    _add_task_selector(show)
    _add_json_arg(show)
    output = task_sub.add_parser("output", help="read the provider's output of a task's last run")
    _add_mission_arg(output, default_mission)
    _add_task_selector(output)
    output.add_argument("--follow", action="store_true", help="follow a log that is still growing; Up pauses, End resumes (needs a terminal)")
    _add_json_arg(output)
    diff = task_sub.add_parser("diff", help="show the files a task's last run changed in the working tree")
    _add_mission_arg(diff, default_mission)
    _add_task_selector(diff)
    _add_json_arg(diff)
    amend = task_sub.add_parser("amend")
    _add_mission_arg(amend, default_mission)
    _add_task_selector(amend)
    _read_prompt_args(amend, "replacement task")
    amend.add_argument("--requeue", action="store_true", help="needed for a task that awaits review: send the old result back and run the amended task again (uses capacity)")
    _add_json_arg(amend)
    task_help = {
        "priority": "change scheduling priority",
        "reorder": "change the ordering value",
        "dependencies": "add a dependency",
        "pause": "hold the task",
        "resume": "make the task eligible again",
        "cancel": "cancel the task",
        "retry": "queue another attempt",
        "approve": "approve a task in REVIEW as complete",
        "verify": "record verification evidence without approving",
        "reject": "send a task awaiting verification back for another attempt",
    }
    for action in ("priority", "reorder", "dependencies", "pause", "resume", "cancel", "retry", "approve", "verify", "reject"):
        cmd = task_sub.add_parser(action, help=task_help[action])
        _add_mission_arg(cmd, default_mission)
        _add_task_selector(cmd)
        if action == "priority":
            cmd.add_argument("--value", metavar="N", type=_db_int, required=True, help="new priority; a HIGHER number runs first, the default is 0 and negative numbers run last (P-numbers are not urgency levels)")
        elif action == "reorder":
            cmd.add_argument("--value", metavar="N", type=_db_int, required=True, help="new ordering value among tasks of equal priority; a LOWER number runs first (the opposite of priority)")
        elif action == "dependencies":
            cmd.add_argument("--add", metavar="N", type=_db_int, required=True, help="number of the task this task must wait for")
        elif action in {"approve", "verify"}:
            cmd.add_argument("--evidence", metavar="TEXT", default="", help="verification evidence, for example a test summary; shown by task show")
            if action == "verify":
                # Deprecated spelling of `task approve`; kept working, hidden from help.
                cmd.add_argument("--approve", action="store_true", help=argparse.SUPPRESS)
        elif action == "reject":
            cmd.add_argument("--reason", metavar="TEXT", required=True, help="why the result is not accepted (up to 2000 characters); the next attempt's prompt quotes it")
            cmd.add_argument("--hold", action="store_true", help="keep the task paused instead of running it again at once (the retry uses capacity); resume it with task resume")
        _add_json_arg(cmd)

    proposals = sub.add_parser("proposal", help="review agent-originated task proposals")
    proposal_sub = proposals.add_subparsers(dest="proposal_action", required=True)
    proposal_create = proposal_sub.add_parser("create", help="record a proposal from an operation")
    _add_mission_arg(proposal_create, default_mission)
    proposal_create.add_argument("--operation", metavar="OPERATION_ID", required=True, dest="source_operation_id", help="ID of the provider operation that made the proposal")
    _read_prompt_args(proposal_create, "proposed task")
    proposal_create.add_argument("--title", metavar="TEXT", default="", help="short title for the proposed task")
    proposal_create.add_argument("--priority", metavar="N", type=_db_int, default=0, help="priority the task gets if accepted; higher runs first (default: 0)")
    proposal_create.add_argument("--depends-on", metavar="IDS", default="", help="comma-separated task numbers the proposed task depends on")
    proposal_create.add_argument("--acceptance", metavar="TEXT", action="append", default=[], help="acceptance criterion for the proposed task; repeat for several")
    _add_json_arg(proposal_create)
    proposal_list = proposal_sub.add_parser("list", help="list proposals")
    _add_mission_arg(proposal_list, default_mission)
    proposal_list.add_argument("--status", choices=("PENDING", "ACCEPTED", "REJECTED", "ALL"), default="PENDING", help="only proposals in this state (default: PENDING)")
    proposal_list.add_argument("--limit", metavar="N", type=_db_int, default=100, help="maximum proposals returned (default: 100)")
    proposal_list.add_argument("--after", metavar="N", type=_db_int, default=0, help="only proposals with a task number above this (default: 0)")
    _add_json_arg(proposal_list)
    for proposal_action in ("accept", "reject"):
        proposal_decision = proposal_sub.add_parser(proposal_action, help=f"{proposal_action} a pending proposal")
        _add_mission_arg(proposal_decision, default_mission)
        decided = proposal_decision.add_mutually_exclusive_group(required=True)
        decided.add_argument("--task", "-t", metavar="N", dest="task", type=_db_int, help="task number of the proposal, as shown by proposal list")
        decided.add_argument("task_number", nargs="?", metavar="N", type=_db_int, action=_positional_into("task"), help="the proposal's task number, same as --task N")
        if proposal_action == "reject":
            proposal_decision.add_argument("--reason", metavar="TEXT", default="", help="why the proposal is rejected")
        _add_json_arg(proposal_decision)

    events = sub.add_parser("events")
    _add_mission_arg(events, default_mission)
    events.add_argument("--after", metavar="EVENT", type=_db_int, default=None, help="oldest first: only events with an ID above this. Event IDs are global, counted across all missions, so a mission's first ID is not 1. Without --after, the newest events are shown")
    events.add_argument("--limit", metavar="N", type=_db_int, default=100, help="maximum events returned: the newest N, or the first N after --after (default: 100)")
    events.add_argument("--since", metavar="AGE", type=_event_age, default=0, help="only events from the last AGE, for example 90s, 10m, 2h or 1d")
    events.add_argument("--follow", "-f", action="store_true", help="keep running and print new events as they arrive; Ctrl-C stops")
    events.add_argument("--verbose", action="store_true", help="include controller bookkeeping (heartbeats, mode, prompt and wake records), which is hidden by default")
    _add_json_arg(events)

    for action in ("start", "pause", "resume", "continue", "stop", "cancel", "attach", "detach"):
        cmd = sub.add_parser(action)
        _add_mission_arg(cmd, default_mission)
        if action in {"attach", "detach"}:
            client_help = (
                "terminal to use when several are attached (for example /dev/pts/3)"
                if action == "attach"
                else "select a writable client to detach; required only when multiple are connected"
            )
            cmd.add_argument("--client", metavar="TTY", help=client_help)
        if action == "start":
            cmd.add_argument("--mode", choices=RUN_MODES, default=None, help="run mode for this start: Native (the provider's own terminal in a station), Background (a station shows the activity log) or Headless (no station); the choice is saved with the mission (default: the mission's saved mode, shown by status)")
            cmd.add_argument("--headless", action="store_true", default=None, help=argparse.SUPPRESS)
            cmd.add_argument("--native", dest="native_tui", action="store_true", default=None, help=argparse.SUPPRESS)
            cmd.add_argument("--providers", metavar="LIST", help="comma-separated providers to rotate through; changes the mission's saved rotation for this and later starts, and the receipt says so (default: the saved rotation)")
            cmd.add_argument("--routes", metavar="LIST", help="comma-separated route IDs to rotate through; changes the mission's saved rotation for this and later starts, and the receipt says so (default: the saved rotation)")
        if action == "pause":
            cmd.add_argument("--after-task", dest="after_task", action="store_true", help="do not pause now: let the provider finish its current invocation, then pause (the same intent as the older steer --kind pause-after-iteration; also applies while the mission waits for capacity)")
        if action == "stop":
            cmd.add_argument("--force", action="store_true", help="terminate owned provider work and the controller now instead of waiting for the provider to exit")
            cmd.add_argument("--after-task", dest="after_task", nargs="?", type=int, const=0, default=None, metavar="TASK", help="do not stop now: stop once task TASK is completed (default: the task that is running now); the same intent as the older steer --kind stop-after-task")
        if action == "cancel":
            cmd.add_argument("--yes", action="store_true", help="confirm without asking; required when there is no terminal to ask on (scripts). Cancelling ends the mission for good and cannot be undone")
        _add_json_arg(cmd)

    missions = sub.add_parser("missions", help="list every mission, live ones first, or pick one to switch to")
    missions.add_argument("--pick", action="store_true", help="open the interactive mission picker (needs a terminal)")
    missions.add_argument("--limit", metavar="N", type=_db_int, default=None, help=f"list at most N missions, live ones first (default: {MISSION_LIST_PAGE}; all with --json)")
    missions.add_argument("--from", dest="from_mission", metavar="ID", help=argparse.SUPPRESS)  # internal: the station's Ctrl-] then M binding passes it
    missions.add_argument("--client", metavar="TTY", help="station client to switch; default: the most recently active one")
    _add_json_arg(missions)

    gc = sub.add_parser("gc", help="close the idle tmux stations of finished missions (stations with a live provider or an attached terminal are kept)")
    _add_json_arg(gc)

    backup = sub.add_parser("backup", help="write a consistent copy of the Bridge database (missions, tasks, events)")
    backup.add_argument("--output", metavar="FILE", required=True, help="new file to write; an existing file is never overwritten")
    _add_json_arg(backup)
    restore = sub.add_parser("restore", help="replace the Bridge database with a backup; the current one is kept beside it")
    restore.add_argument("--input", metavar="FILE", required=True, help="backup written by llm-bridge backup")
    _add_json_arg(restore)
    worker = sub.add_parser("worker", help="optional: connect this host to a Bridge coordinator (off unless enrolled and run)")
    worker_sub = worker.add_subparsers(dest="worker_action", required=True)
    worker_enroll = worker_sub.add_parser("enroll", help="store the coordinator URL, CA and worker token for this host")
    worker_enroll.add_argument("--name", help="worker name used as the remote target (default: short host name)")
    worker_enroll.add_argument("--coordinator", required=True, metavar="URL", help="coordinator base URL, for example wss://bridge.home.arpa:8443 (https:// is accepted for the same coordinator)")
    worker_enroll.add_argument("--token-file", required=True, metavar="PATH", help="file holding the worker token printed by the coordinator ('-' reads stdin)")
    worker_enroll.add_argument("--ca-file", metavar="PATH", help="coordinator CA certificate (PEM); default: the system trust store")
    worker_enroll.add_argument("--allow-control", action="store_true", help="accept mutating remote commands and terminal input; without it the worker is read-only remotely")
    worker_enroll.add_argument("--allow-terminal-view", action="store_true", help="let a read-only worker stream its mission terminals for viewing (implied by --allow-control)")
    worker_enroll.add_argument("--allow-create", action="store_true", help="allow remote mission creation (requires a path inside an enrolled --allow-root)")
    worker_enroll.add_argument("--allow-root", action="append", default=[], metavar="DIR", help="canonical directory allowed for remote creation and restore inputs; repeat as needed")
    worker_enroll.add_argument("--allow-power", action="store_true", help="allow remote machine power operations")
    worker_enroll.add_argument("--allow-restore", action="store_true", help="allow remote restore operations (requires an enrolled --allow-root)")
    worker_enroll.add_argument("--client-cert-file", metavar="PATH", help="coordinator-issued worker client certificate PEM for optional mTLS")
    worker_enroll.add_argument("--client-key-file", metavar="PATH", help="matching worker client private key PEM for optional mTLS")
    _add_json_arg(worker_enroll)
    worker_rotate = worker_sub.add_parser("rotate-token", help="install a coordinator-staged worker token without re-enrolling")
    worker_rotate.add_argument("--token-file", required=True, metavar="PATH", help="file holding the one-time token from bridge-coordinator worker rotate-token")
    _add_json_arg(worker_rotate)
    worker_status = worker_sub.add_parser("status", help="show this host's enrolment without the token")
    _add_json_arg(worker_status)
    worker_sub.add_parser("run", help="run the worker in the foreground until interrupted")
    worker_audit = worker_sub.add_parser("audit", help="show this worker's security audit log (no tokens or terminal content)")
    worker_audit.add_argument("--limit", metavar="N", type=_db_int, default=50, help="newest entries shown (default: 50)")
    _add_json_arg(worker_audit)
    worker_install = worker_sub.add_parser("install-service", help="install and start a systemd user unit that runs the worker")
    worker_install.add_argument("--print", action="store_true", help="print the unit instead of installing it")
    worker_sub.add_parser("uninstall-service", help="stop and remove the worker's systemd user unit")
    worker_sub.add_parser("unenroll", help="delete this host's enrolment, token and coordinator CA")
    worker_list = worker_sub.add_parser("list", help="list the workers registered with the configured coordinator")
    _add_json_arg(worker_list)

    remote = sub.add_parser("remote", help="configure this CLI to reach other workers through a coordinator (used by --target)")
    remote_sub = remote.add_subparsers(dest="remote_action", required=True)
    remote_login = remote_sub.add_parser("login", help="store the coordinator URL, CA and API token")
    remote_login.add_argument("--coordinator", required=True, metavar="URL", help="coordinator URL, for example https://bridge.home.arpa:8443 (wss:// is accepted for the same coordinator)")
    remote_login.add_argument("--token-file", required=True, metavar="PATH", help="file holding an API token from bridge-coordinator token create ('-' reads stdin)")
    remote_login.add_argument("--ca-file", metavar="PATH", help="coordinator CA certificate (PEM); default: the system trust store")
    remote_status = remote_sub.add_parser("status", help="show the configured coordinator without the token")
    _add_json_arg(remote_status)
    remote_sub.add_parser("logout", help="delete the stored coordinator URL, CA and API token")

    finish = sub.add_parser("finish", help="ask the native provider to exit so its task moves to verification")
    _add_mission_arg(finish, default_mission)
    _add_json_arg(finish)

    doctor = sub.add_parser("doctor")
    _add_mission_arg(doctor, default_mission, inferred=False)
    doctor.add_argument("--include-config", action="store_true", help="with --json, include the mission's saved copy of the llm-tools config (large; left out by default)")
    doctor.add_argument("--bundle", nargs="?", const="", default=None, metavar="PATH", help="write a redacted diagnostic archive for bug reports (default: llm-bridge-bundle-<UTC time>.tar.gz here; never overwrites)")
    _add_json_arg(doctor)

    theme = sub.add_parser("theme", help="show the detected terminal background, the active colour theme and a swatch per role")
    theme.add_argument("--theme", dest="theme_name", choices=THEME_CHOICES, default="auto",
                       help="preview another theme instead of the active one")
    theme.add_argument("--palette-size", dest="colors", type=int, choices=(8, 16, 256), help="preview a terminal that shows this many colours")
    theme.add_argument("--colors", dest="colors", type=int, choices=(8, 16, 256), help=argparse.SUPPRESS)  # older name of --palette-size
    theme.add_argument("--colour", dest="color", choices=("auto", "always", "never"), default="auto", help="render swatches with colour (default: when stdout is a terminal)")
    theme.add_argument("--color", dest="color", choices=("auto", "always", "never"), default=argparse.SUPPRESS, help=argparse.SUPPRESS)  # US spelling of --colour
    _add_json_arg(theme)

    console = sub.add_parser("console", help=argparse.SUPPRESS)
    _add_mission_arg(console, default_mission, required=True, inferred=False)
    control = sub.add_parser("control", help="open the full Bridge control view")
    _add_mission_arg(control, default_mission)
    usage_view = sub.add_parser("usage", help="show the full llm-usage dashboard inside Bridge")
    _add_mission_arg(usage_view, default_mission)

    station = sub.add_parser("station", help="choose the station shown in the control view")
    _add_mission_arg(station, default_mission)
    station.add_argument("name", help="provider or route name from the mission's rotation")
    _add_json_arg(station)

    rotation = sub.add_parser("rotation", help="show or change the mission's saved provider rotation")
    _add_mission_arg(rotation, default_mission)
    rotation_change = rotation.add_mutually_exclusive_group()
    rotation_change.add_argument("--set", dest="set_list", metavar="LIST", help="comma-separated provider or route names, tried in this order, for example claude,codex; provider names replace the saved rotation, and a route name from the config saved with the mission switches the mission to routes")
    rotation_change.add_argument("--routes", metavar="LIST", help=argparse.SUPPRESS)  # older form of --set with route names
    rotation_change.add_argument("--providers", metavar="LIST", help=argparse.SUPPRESS)  # older form of --set with provider names; clears saved routes
    _add_json_arg(rotation)

    steer = sub.add_parser("steer", help="queue typed steering for a task or safe boundary")
    _add_mission_arg(steer, default_mission)
    steer.add_argument("--kind", required=True, choices=("replace", "review", "verify", "pause-after-iteration", "stop-after-task", "handoff"), metavar="{handoff,review}", help="handoff: switch to another provider at the next safe point; review: add a task that reviews TASK after it finishes")
    _add_task_selector(steer, required=False, note="; required for review")
    steer.add_argument("--to", dest="route", metavar="PROVIDER", help="provider (or route) from the mission's rotation to hand over to; required for handoff")
    steer.add_argument("--route", dest="route", metavar="NAME", help=argparse.SUPPRESS)  # older name of --to
    steer.add_argument("--file", "--prompt-file", dest="prompt_file", metavar="PATH", help="read the review prompt from PATH")
    steer.add_argument("--prompt", help="the review prompt as literal text (default: a standard review of the task)")
    _add_json_arg(steer)

    station_gc = sub.add_parser("_station-gc", help=argparse.SUPPRESS)
    station_gc.add_argument("--mission", required=True)
    station_gc.add_argument("--exclude-client", action="append", default=[])
    controller = sub.add_parser("_controller", help=argparse.SUPPRESS)
    _add_mission_arg(controller, default_mission, required=True, inferred=False)
    controller.add_argument("--headless", action="store_true")
    controller.add_argument("--native", dest="native_tui", action="store_true")
    controller.add_argument("--background", action="store_true")
    controller.add_argument("--startup-token", default="")
    controller.add_argument("--providers", default=None)
    controller.add_argument("--routes", default=None)
    if only is None:  # decoration only changes help text
        from .bridge_help import decorate_parser

        decorate_parser(parser)
    _disable_abbreviations(parser)
    return parser


class _FastParseFailed(Exception):
    """Raised instead of exiting when the single-command parser rejects the arguments."""


def _parse_arguments(arguments: list[str]) -> argparse.Namespace:
    """Parse a command line; for ``mission create`` the words after ``--`` are the verifier command."""
    if arguments[:2] == ["mission", "create"] and "--" in arguments:
        cut = arguments.index("--")
        parsed = _parse_arguments_core(arguments[:cut])
        parsed.verifier_after_dashes = arguments[cut + 1:]
        return parsed
    return _parse_arguments_core(arguments)


def _parse_arguments_core(arguments: list[str]) -> argparse.Namespace:
    """Parse a command line, building only the requested command's parser when that is enough.

    Anything the single-command parser cannot accept (an unknown command, a request for help, a
    usage error) is handed to the full parser, which then produces the usual output.
    """
    command = arguments[0] if arguments else ""
    if command and not command.startswith("-") and not any(word in {"-h", "--help"} for word in arguments):
        import argparse

        class _FastParser(argparse.ArgumentParser):
            def error(self, message: str) -> Any:
                raise _FastParseFailed(message)

        try:
            parser = _build_parser(parser_class=_FastParser, only=command)
            if command in {name for action in parser._actions if isinstance(action, argparse._SubParsersAction) for name in action.choices}:
                return parser.parse_args(arguments)
        except _FastParseFailed:
            pass
    return _build_parser().parse_args(arguments)
