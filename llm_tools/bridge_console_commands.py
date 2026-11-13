"""Console command parsing, completion and feedback."""

from __future__ import annotations

import ast
import functools
import json
import re
import shlex
import unicodedata
from dataclasses import dataclass
from typing import Callable
from . import bridge_format
from .bridge_glyphs import glyph
from .bridge_actions import ACTIONS
from .bridge_event_text import _SEP, _age, _compact_pairs, _dict, _event_kind, _int_or_none, _known, _text


PALETTE_COMMANDS = tuple(action.command for action in ACTIONS)


# F1-F9 select the views in this order. Activity and Sessions (history) sit
# together after the working views; Health (diagnostics) is next to Actions.
# Names used before the 1.3 rename stay accepted everywhere a view is named.
# Words typed in the command line that switch view (the rest are CLI commands).
_VIEW_COMMANDS = {
    "overview": "Overview", "control": "Overview", "tasks": "Tasks", "orders": "Tasks", "providers": "Providers", "activity": "Activity",
    "sessions": "Sessions", "health": "Health", "actions": "Actions", "usage": "Usage", "agents": "Agents",
    "palette": "Actions", "log": "Activity", "systems": "Health", "stations": "Providers",
}


_REFRESH_SECONDS = 0.25


_ACTIVITY_POLL_SECONDS = 2.0


_FULL_REFRESH_SECONDS = 5.0  # a full snapshot at least this often: lock files, usage and clocks change without a database commit


_IDLE_BACKOFF_SECONDS = 10.0  # without input for this long the change probe slows to once a second


_IDLE_REFRESH_SECONDS = 1.0


_STATUS_SECONDS = 8.0  # how long a command result stays in the bottom row


_OUTPUT_IDLE_THRESHOLD_SECONDS = 30


_RECALL_HINT = "Ctrl-P recalls the command"  # the command line is empty after a failure, so Up would move the body selection


_MAX_COMMAND_CHARS = 4096


_MAX_OUTPUT_LINES = 80


_MAX_OUTPUT_LINE_CHARS = 2000


_MAX_RESPONSE_CHARS = 24000


_MAX_RESPONSE_LINES = _MAX_OUTPUT_LINES


_MAX_TASK_ROWS = 200


_TASK_PAGE_SIZE = _MAX_TASK_ROWS


_MAX_STATION_ROWS = 32


_MAX_EVENT_ROWS = 80


_MAX_DETAIL_TEXT = 16000


_MAX_DETAIL_ITEMS = 12


_FILE_VIEWER_KEYS = frozenset({"s", "c", "e", "m", "w", "h", "KEY_LEFT", "KEY_RIGHT"})


_SELECTABLE_VIEWS = frozenset({"Tasks", "Providers", "Actions", "Agents"})


_BRIDGE_LABEL = 12


_SYSTEM_LABEL = 22


_SESSION_COLUMNS = ("op", "task", "provider", "model", "status", "native session", "restoration")


_TASK_VERB_PHRASES = {
    "add": "added", "amend": "amended", "priority": "priority changed", "reorder": "reordered",
    "dependencies": "dependencies updated", "pause": "paused", "resume": "resumed", "cancel": "cancelled",
    "retry": "queued for another attempt", "verify": "verified", "approve": "approved", "reject": "sent back",
}


_TASK_ACK = re.compile(r"^task(?: \S+)? (?:" + "|".join(re.escape(item) for item in _TASK_VERB_PHRASES.values()) + r")")


# Options whose value is a private path or an id the operator never typed.
_PRIVATE_OPTIONS = frozenset({"--file", "--prompt-file", "--mission"})


def task_number(task: dict[str, object]) -> object:
    """The number the operator knows a task by: the per-mission ordinal when present, else its id."""
    return task.get("ordinal", task.get("id"))


def _task_ref(snapshot: dict[str, object], number: object) -> list[str]:
    """CLI arguments naming the task shown as `number`.

    `--task N` is the mission's own task number (the snapshot's `ordinal`). A
    snapshot without ordinals can only name a task by its global id, so those
    tasks are addressed with `--task-id`.
    """
    for task in _items(snapshot.get("tasks")):
        if str(task_number(task)) == str(number):
            if "ordinal" not in task and task.get("id") is not None:
                return ["--task-id", str(task["id"])]
            break
    return ["--task", str(number)]


def _applied_label(command: str, result: dict[str, object]) -> str:
    """What was applied, in operator words; never a draft path or the mission id.

    `task add --file /…/drafts/task-x.md` becomes `Task 4 added`, and an amend
    becomes `Task 2 amended (revision 3)`. Other commands keep their words with
    file and mission options removed.
    """
    try:
        words = shlex.split(_strip_slash(command))
    except ValueError:
        words = _strip_slash(command).split()
    if len(words) >= 2 and words[0] == "task" and words[1] in _TASK_VERB_PHRASES:
        # The command's own `--task N` is the mission's number; a result's `task_id` or `id` is the global id.
        number = words[words.index("--task") + 1] if "--task" in words and words.index("--task") + 1 < len(words) else result.get("ordinal")
        if number is None and "--task-id" in words and words.index("--task-id") + 1 < len(words):
            number = words[words.index("--task-id") + 1]
        if number is None:
            number = result.get("task_id", result.get("id"))
        text = "Task" + (f" {_text(number, 12)}" if number is not None else "") + f" {_TASK_VERB_PHRASES[words[1]]}"
        revision = result.get("revision")
        if words[1] == "amend" and isinstance(revision, int) and not isinstance(revision, bool):
            text += f" (revision {revision})"
        return text
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in _PRIVATE_OPTIONS:
            skip = True
        elif not word.startswith(tuple(f"{name}=" for name in _PRIVATE_OPTIONS)):
            kept.append(word)
    return " ".join(kept) or _text(command, 120)


def _cut_text(value: object, limit: int) -> str:
    """`_text` that marks a cut with an ellipsis, so clipped detail is never mistaken for the whole."""
    clean = _text(value, limit + 1)
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 1)].rstrip() + glyph("ellipsis")


def _command_feedback(command: str, response: str) -> str:
    """Turn known durable command envelopes into compact human acknowledgments."""
    try:
        payload = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and "result" in payload and payload.get("status") in {"APPLIED", "REJECTED", "FAILED"}:
        status = str(payload["status"])
        result = _dict(payload.get("result"))
        if status == "APPLIED":
            detail = result.get("detail")
            label = _text(_applied_label(command, result), 120)
            if label.startswith("Task ") and not result.get("kind"):
                return _text(f"{label} — {detail}" if detail else label, _MAX_OUTPUT_LINE_CHARS)
            if result.get("kind"):
                label = _text(result.get("kind"), 80) + " steering"
            if detail:
                return _text(f"Applied: {label} — {detail}", _MAX_OUTPUT_LINE_CHARS)
            return _text(f"Applied: {label}", _MAX_OUTPUT_LINE_CHARS)
        reason = result.get("error") or result.get("detail") or status.lower()
        return _text(f"Rejected: {reason}", _MAX_OUTPUT_LINE_CHARS)

    rendered = json.dumps(payload, ensure_ascii=False, indent=2) if payload is not None else response
    lines = rendered.splitlines() or [""]
    bounded: list[str] = []
    char_count = 0
    truncated = False
    for line in lines:
        if len(bounded) >= _MAX_RESPONSE_LINES or char_count >= _MAX_RESPONSE_CHARS:
            truncated = True
            break
        clean = "".join(
            char if unicodedata.category(char) not in {"Cc", "Cs"} or char == "\t" else " "
            for char in line
        ).replace("\t", "    ")
        remaining = _MAX_RESPONSE_CHARS - char_count
        limit = min(_MAX_OUTPUT_LINE_CHARS, remaining)
        if len(clean) > limit:
            clean = clean[: max(0, limit - 1)] + "…"
            truncated = True
        bounded.append(clean)
        char_count += len(clean) + 1
        if truncated:
            break
    if len(bounded) < len(lines):
        truncated = True
    if truncated:
        notice = "[response truncated to fit the Bridge console limit]"
        if len(bounded) >= _MAX_RESPONSE_LINES:
            bounded[-1] = notice
        else:
            bounded.append(notice)
    return "\n".join(bounded)


def console_words(command_line: str) -> list[str]:
    """`bridge.console_words`, imported when first used (bridge imports this module lazily)."""
    from .bridge import console_words as split

    return split(command_line)


def expand_reject_alias(words: list[str], waiting: Callable[[], list[int]]) -> tuple[list[str] | None, str | None]:
    from .bridge import expand_reject_alias as expand

    return expand(words, waiting)


def _strip_slash(command: str) -> str:
    """Drop the Codex/Claude-style `/` from a leading slash command."""
    text = command.strip()
    if len(text) > 1 and text[0] == "/" and (text[1].isalpha() or text[1] == "?"):
        return text[1:]
    return text


def _confirmation_reason(command: str) -> str | None:
    """Return the consequence that needs a deliberate second keystroke."""
    try:
        words = console_words(_strip_slash(command))
    except ValueError:
        return None
    if words and words[0] == "cancel":
        return "Cancel this mission and its unfinished tasks? This cannot be undone."
    if words and words[0] == "stop" and "--force" in words:
        return "Force stop this mission's owned active work?"
    if words and words[0] == "finish":
        return "Ask the provider to exit now? Unsaved provider state is lost; the task moves to verification."
    if words[:2] == ["task", "cancel"]:
        return "Cancel this task and discard its remaining work?"
    if words[:2] == ["task", "reject"] and "--task" in words:
        reason = next((word[len("--reason="):] for word in words if word.startswith("--reason=")), "")
        return f"Send task {words[words.index('--task')+1]} back for another attempt? Reason: {reason[:120]}"
    if _is_approval_command(words):
        kind, value = _approval_target(words)
        label = f"task {value}" if kind == "task" else f"task with id {value}" if kind == "id" else "(select a task)"
        return f"Approve verification and mark {label} complete?"
    return None


def _is_reject_command(command: str) -> bool:
    """Whether ``command`` is the expanded ``task reject --task N …`` the console confirms."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return words[:2] == ["task", "reject"] and "--task" in words and "--hold" not in words


def _is_approval_command(words: list[str]) -> bool:
    """`task approve …`, or the deprecated spelling `task verify … --approve`."""
    return words[:2] == ["task", "approve"] or (words[:2] == ["task", "verify"] and "--approve" in words)


def _approval_target(words: list[str]) -> tuple[str, str]:
    """(`task`, number), (`id`, global id) or (``, ``) for the task an approval command names."""
    for option, kind in (("--task", "task"), ("--task-id", "id")):
        if option in words and words.index(option) + 1 < len(words):
            return kind, words[words.index(option) + 1]
    return "", ""


def _approval_task(command: str) -> tuple[str, str] | None:
    """(kind, value) of the task an approval command (`task approve …`) names, else None."""
    try:
        words = console_words(_strip_slash(command))
    except ValueError:
        return None
    if _is_approval_command(words):
        target = _approval_target(words)
        return target if target[0] else None
    return None


def _approval_summary(snapshot: dict[str, object], target: tuple[str, str], width: int = 100) -> list[str]:
    """What the operator is about to approve: title, latest evidence, the last run's exit status and workspace change.

    Only facts the projection holds are shown. The workspace line comes from the
    repository fingerprints recorded before and after the task's last provider
    run, with the changed file names the controller recorded when the run
    ended (none for a folder that is not a repository).
    """
    kind, value = target
    task = next((item for item in _items(snapshot.get("tasks"))
                 if (str(item.get("id")) if kind == "id" else str(task_number(item))) == value), None)
    if task is None:
        return [f"Task {value} is not in the loaded list; check Tasks before approving."]
    number = str(task_number(task))
    lines = [f"Task {number}: {_cut_text(task.get('title') or task.get('prompt') or 'Untitled task', width)}"]
    raw_evidence = task.get("evidence")
    evidence = [item for item in raw_evidence if _known(item)] if isinstance(raw_evidence, list) else []
    if evidence:
        shown = evidence[-3:]
        lines.append(f"Evidence ({len(evidence)}):" if len(evidence) == len(shown) else f"Evidence (latest {len(shown)} of {len(evidence)}):")
        lines += [f"  {_cut_text(_evidence_text(item), width)}" for item in shown]
    else:
        lines.append("Evidence: none recorded; read the provider output before approving.")
    task_id = str(task.get("id"))
    runs = [op for op in _items(snapshot.get("operations")) if str(op.get("task_id")) == task_id]
    if runs:
        last = max(runs, key=lambda op: str(op.get("created_at") or ""))
        who = _text(last.get("route_id") or last.get("station") or last.get("provider") or "provider", 40)
        code = last.get("exit_code")
        lines.append(f"Last run: {who}" + (f", {bridge_format.exit_short(_text(code, 8))}" if code is not None else "") + (f", {_text(last['status'], 20).lower()}" if _known(last.get("status")) else ""))
    progress = [
        _dict(event.get("data")) for event in _items(snapshot.get("events"))
        if _event_kind(event) == "operation-progress"
        and task_id in {str(event.get("task_id")), str(_dict(event.get("data")).get("task_id"))}
    ]
    known = [data for data in progress if data.get("before") and data.get("after")]
    if known:
        lines.append(_cut_text(_workspace_line(known[-1]), width))
    return lines


_WORKSPACE_FILES_SHOWN = 5


def _workspace_line(progress: dict[str, object]) -> str:
    """`Workspace: 1 file changed: hello.txt`, or the plain change/no-change verdict when no names were recorded."""
    if progress["before"] == progress["after"]:
        return "Workspace: unchanged by the run"
    names = progress.get("changed_files")
    total = _int_or_none(progress.get("changed_total"))
    if not isinstance(names, list) or not total:
        return "Workspace: changed during the run"
    shown = [_text(name, 80) for name in names[:_WORKSPACE_FILES_SHOWN]]
    more = total - len(shown)
    line = f"Workspace: {total} file{'s' if total != 1 else ''} changed: {', '.join(shown)}" + (f" and {more} more" if more > 0 else "")
    earlier = _int_or_none(progress.get("already_modified"))
    return line + (f" ({earlier} already modified before the run)" if earlier else "")


def _last_run(snapshot: dict[str, object], task: dict[str, object]) -> dict[str, object] | None:
    """The newest operation the projection holds for ``task``."""
    task_id = str(task.get("id"))
    runs = [op for op in _items(snapshot.get("operations")) if str(op.get("task_id")) == task_id]
    return max(runs, key=lambda op: str(op.get("created_at") or "")) if runs else None


def _last_run_line(snapshot: dict[str, object], task: dict[str, object]) -> str | None:
    """`Last run: claude · exit 0 · 12s · o output · d diff` for the Tasks inspect pane, or None before any run."""
    from . import common

    last = _last_run(snapshot, task)
    if last is None:
        return None
    parts = [_text(last.get("route_id") or last.get("station") or last.get("provider") or "provider", 40)]
    if last.get("exit_code") is not None:
        parts.append(bridge_format.exit_short(_text(last["exit_code"], 8)))
    started = common.parse_epoch(last.get("created_at"), use_date=False)
    ended = common.parse_epoch(last.get("updated_at"), use_date=False)
    if started is not None and ended is not None and ended >= started and str(last.get("status")) not in {"RUNNING", "PREPARED", "INTENT"}:
        parts.append(_age(int(ended - started), note=False))
    return "Last run: " + _SEP.join([*parts, "o output", "d diff"])


# Console-only shortcuts that expand to public commands.
_CONSOLE_ALIASES = ("approve", "reject", "leave", "overview", "providers", "activity", "health", "actions", "sessions", "home")


def _expand_console_alias(command: str, snapshot: dict[str, object]) -> tuple[str, str | None]:
    """Expand `approve [N]` to `task approve --task N`.

    Returns the command to run and an error message. Without N the single
    task that awaits verification is used.
    """
    try:
        words = console_words(_strip_slash(command))
    except ValueError:
        return command, None
    if words and words[0] == "reject":
        expanded, problem = expand_reject_alias(
            words, lambda: [int(str(task_number(task))) for task in _items(snapshot.get("tasks")) if str(task.get("status", "")).upper() == "VERIFYING"]
        )
        if expanded is None:
            return command, problem
        if "--task" in expanded:  # name the task by id when the snapshot has no task numbers
            position = expanded.index("--task")
            expanded[position:position + 2] = _task_ref(snapshot, expanded[position + 1])
        return shlex.join(expanded), None
    if not words or words[0] != "approve":
        return command, None
    if len(words) > 2 or (len(words) == 2 and not words[1].isdecimal()):
        return command, "error: usage: approve [TASK-NUMBER]"
    if len(words) == 2:
        task_id = words[1]
    else:
        waiting = [str(task_number(task)) for task in _items(snapshot.get("tasks")) if str(task.get("status", "")).upper() == "VERIFYING"]
        if len(waiting) != 1:
            hint = f"tasks awaiting review: {', '.join(waiting)}" if waiting else "no task awaits review"
            return command, f"error: approve needs a task number · {hint}"
        task_id = waiting[0]
    return shlex.join(["task", "approve", *_task_ref(snapshot, task_id)]), None


def _command_completions(command: str) -> list[str]:
    """Complete public command/option names from the authoritative parser.

    A leading `/` (Codex/Claude style) is accepted and kept: `/pa` completes
    to `/pause`.
    """
    if command.startswith("/"):
        return ["/" + item for item in _command_completions(command[1:])]
    import argparse
    from .bridge import _build_parser
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    trailing = bool(command) and command[-1].isspace()
    prefix = "" if trailing or not words else words[-1]
    complete = words if trailing else words[:-1]
    parser = _build_parser(default_mission="current")
    for word in complete:
        groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
        if groups and word in groups[0].choices:
            parser = groups[0].choices[word]
        elif word.startswith("-"):
            break
    groups = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    candidates = list(groups[0].choices) if groups and not prefix.startswith("-") else list(parser._option_string_actions)
    if not complete and groups and not prefix.startswith("-"):
        candidates.extend(name for name in _CONSOLE_ALIASES if name not in candidates)
    base = command if trailing else command[:-len(prefix)] if prefix else command
    return [base + value for value in candidates if value.startswith(prefix) and value not in {"console", "_controller"} and not value.startswith("_")]


# Commands that lead the list shown when `/` is typed; the rest follow in parser order.
_SLASH_FIRST = ("start", "approve", "reject", "resume", "pause", "stop", "status", "attach", "task", "steer", "help")


_SLASH_ALIAS_TEXT = {
    "approve": "Approve a task that awaits review (asks first)",
    "reject": "Send a task back with a reason",
    "leave": "Leave the provider terminal; work keeps running",
    "resume": "Continue after a pause or a stop",
    "overview": "Show the Overview view",
    "providers": "Show the Providers view",
    "activity": "Show the Activity view",
    "sessions": "Show the Sessions view",
    "health": "Show the Health view",
    "actions": "Show the Actions view",
    "home": "Pick another mission",
    "backup": "Copy the Bridge database to a file",
    "restore": "Replace the Bridge database from a backup",
    "worker": "Connect this host to a coordinator (optional)",
    "remote": "Reach other hosts through a coordinator",
    "theme": "Show the colour theme and a swatch per role",
}


@functools.lru_cache(maxsize=1)
def _slash_table() -> tuple[tuple[str, str], ...]:
    """Every slash command with its one-line description, the most used first.

    Names come from the same completion source the Tab key uses. A description is
    the command table of `llm-bridge help`, else the Actions registry, else the
    console-only shortcut text.
    """
    from .bridge_help import COMMANDS

    effects = {action.command.split()[0]: action.effect for action in ACTIONS if action.command.split()[0] != "task"}  # "task" has one action row but many subcommands
    names = [item[1:] for item in _command_completions("/")]
    ordered = [name for name in _SLASH_FIRST if name in names] + [name for name in names if name not in _SLASH_FIRST]
    return tuple(
        ("/" + name, effects.get(name) or _SLASH_ALIAS_TEXT.get(name) or COMMANDS.get(name, ("",))[0].split(";")[0])
        for name in ordered
    )


def _slash_matches(command: str) -> list[tuple[str, str]] | None:
    """The slash commands whose names start with the typed text, or None when no command name is being typed."""
    if not command.startswith("/") or any(char.isspace() for char in command):
        return None
    return [(name, text) for name, text in _slash_table() if name.startswith(command)]


@dataclass(frozen=True)
class _CommandResult:
    """Dispatcher feedback plus a positive durable-ack bit for draft cleanup."""

    text: str
    outcome: str = "unknown"


def _items(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_dict(item) for item in value if isinstance(item, dict)]


def _evidence_text(item: object) -> str:
    """One evidence item as text: its `text` when it is a record, else `key=value` words, else the text itself."""
    value = item if isinstance(item, dict) else None
    if value is None:
        text = _cut_text(item, 240)
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
            except ValueError:
                try:
                    parsed = ast.literal_eval(text)
                except Exception:  # any parser failure (MemoryError, RecursionError, TypeError, ...) leaves the text as is
                    return text
            value = parsed if isinstance(parsed, dict) else None
        if value is None:
            return text
    recorded = value.get("text")
    if isinstance(recorded, str) and recorded.strip():
        return _cut_text(recorded, 240)
    return " ".join(_compact_pairs(value, limit=6)) or _cut_text(item, 240)
