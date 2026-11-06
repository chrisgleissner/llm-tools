"""The sentences that tell the operator what to do next, in one place and for two surfaces.

``llm-bridge status`` (surface ``cli``) names shell commands: ``run: llm-bridge start --mission ID``.
The console (surface ``console``) names slash commands: ``type /start``. Both surfaces use the
same wording otherwise, so a fact reads the same in the Overview, in ``/status`` and in the shell.

The console runs commands in a child process. It sets ``LLM_BRIDGE_SURFACE=console`` in that
child's environment, so ``/status`` inside the console renders slash forms with no extra argument.
Standard library only.
"""

from __future__ import annotations

import os
from typing import Literal

Surface = Literal["cli", "console"]

SURFACE_ENV = "LLM_BRIDGE_SURFACE"

# What the Next row says when no task is waiting to start (the same text in the CLI and the console).
NEXT_NOTHING = "nothing ready"


def current_surface() -> Surface:
    """``console`` when this process was started by the Bridge console, else ``cli``."""
    return "console" if os.environ.get(SURFACE_ENV) == "console" else "cli"


def command_text(surface: Surface, command: str, mission_id: str = "", *arguments: str) -> str:
    """How to run ``command``: ``/start 3`` in the console, ``llm-bridge start --mission ID 3`` in a shell."""
    if surface == "console":
        return " ".join(("/" + command, *arguments))
    mission = f" --mission {mission_id}" if mission_id else ""
    return " ".join((f"llm-bridge {command}{mission}", *arguments))


def controller_hint(state: str, surface: Surface, mission_id: str = "") -> str | None:
    """The Controller row for a mission that has no controller to wait for, or ``None`` for other states."""
    if state == "CREATED":
        return "not started — " + _run_or_type(surface, command_text(surface, "start", mission_id))
    if state == "STOPPED":
        if surface == "console":
            return "stopped — type /start or /resume"
        return "stopped — " + _run_or_type(surface, command_text(surface, "start", mission_id))
    return None


def finished_next(status: str, surface: Surface, mission_id: str = "") -> str | None:
    """The Next row of a completed or cancelled mission: what the operator can do now, or ``None`` for other states."""
    word = {"COMPLETED": "Done", "CANCELLED": "Cancelled"}.get(status)
    if word is None:
        return None
    if surface == "console":
        return f"{word} \u00b7 /missions switches to another mission \u00b7 Ctrl-D leaves the controls"
    return f"{word} \u00b7 llm-bridge missions lists your missions \u00b7 llm-bridge mission create starts a new one"


def approval_row(surface: Surface, numbers: list[int] | list[str], mission_id: str = "") -> str:
    """The Needs you row for tasks that await the operator's decision (the first one is named in the command)."""
    first = str(numbers[0])
    if len(numbers) == 1:
        subject = f"task {first} awaits your review"
    else:
        subject = f"tasks {', '.join(str(number) for number in numbers)} await your review"
    if surface == "console":
        return f"{subject} · type /approve {first} (or F7 Actions)"
    approve = command_text(surface, "task approve", mission_id, "--task", first)
    reject = command_text(surface, "task reject", mission_id, "--task", first, "--reason …")
    return f"{subject} · {approve} (or reject: {reject})"


def proposals_row(surface: Surface, pending: int, mission_id: str = "") -> str:
    """The Needs you row for agent-proposed tasks that await a decision."""
    plural = "s" if pending != 1 else ""
    if surface == "console":
        return f"{pending} agent task proposal{plural} to review · type /proposals"
    return f"{pending} proposed task{plural} await your decision · {command_text(surface, 'proposal list', mission_id)}"


def _run_or_type(surface: Surface, command: str) -> str:
    return f"type {command}" if surface == "console" else f"run: {command}"
