"""Operator actions shown in the Actions view; argparse remains the command grammar.

Each action carries the slash command (or key) that performs it, so the list
teaches the command line. `actions_for` orders and filters the registry by the
mission and task state, so the first row is what the operator most likely needs.
"""
from dataclasses import dataclass, replace

from collections.abc import Mapping

from .bridge_states import ENDED_MISSION_STATUSES



@dataclass(frozen=True)
class Action:
    command: str          # dispatched to the command line
    label: str
    effect: str           # plain-words result
    task: bool = False    # needs a selected task
    mutable: bool = False
    slash: str = ""       # what the operator types or presses; "/" + command when empty
    handler: str = ""     # console-side handling instead of dispatching: "compose", "amend" or "prefill"

    @property
    def typed(self) -> str:
        return self.slash or "/" + self.command.split(" --")[0]


# key: (action, {mission status: rank}); lower ranks come first. A status that
# is missing from the mapping hides the action in that state.
_LIVE = ("CREATED", "RUNNING", "PAUSED", "STOPPING", "STOPPED")

_REGISTRY: tuple[tuple[Action, Mapping[str, float]], ...] = (
    (Action("start", "Start", "Launch the provider on the first ready task", mutable=True),
     {"CREATED": 0, "STOPPED": 0}),
    (Action("resume", "Resume", "Continue after a pause or a stop", mutable=True),
     {"PAUSED": 0, "STOPPED": 1}),
    (Action("attach", "Attach", "Open the provider terminal; Ctrl-] P comes back"),
     {"RUNNING": 0, "PAUSED": 2}),
    (Action("pause", "Pause", "Hold after the current provider turn", mutable=True),
     {"RUNNING": 1}),
    (Action("task add --help", "Add task", "Write a new task for the queue", mutable=True, slash="Ctrl-E", handler="compose"),
     {"CREATED": 1, "RUNNING": 2, "PAUSED": 3, "STOPPED": 2}),
    (Action("task amend --task {task}", "Amend task", "Edit the selected task's text", task=True, mutable=True, slash="Ctrl-R", handler="amend"),
     {"CREATED": 2, "RUNNING": 5, "PAUSED": 4, "STOPPED": 3}),
    (Action("steer --help", "Steer", "Redirect work at a safe point (lists options)"),
     {"RUNNING": 3, "PAUSED": 6}),
    (Action("stop", "Stop", "Stop after the current step; progress is kept", mutable=True),
     {"RUNNING": 4, "PAUSED": 5, "STOPPING": 0}),
    (Action("proposal list", "Proposals", "Review tasks the agent suggested", slash="/proposals"),
     {status: 6 for status in _LIVE}),
    (Action("status", "Status", "Print the mission state, blockers and queue"),
     {status: 7 for status in (*_LIVE, *ENDED_MISSION_STATUSES)} | {status: 0 for status in ENDED_MISSION_STATUSES}),
    (Action("detach", "Detach", "Leave the provider terminal; work keeps running"),
     {"RUNNING": 8, "PAUSED": 8}),
    (Action("cancel", "Cancel mission", "Cancel mission and unfinished tasks (asks first)", mutable=True),
     {status: 9 for status in _LIVE}),
    (Action("help", "Help", "Read the offline quick-start"),
     {status: 10 for status in (*_LIVE, *ENDED_MISSION_STATUSES)} | {status: 1 for status in ENDED_MISSION_STATUSES}),
)

ACTIONS = tuple(action for action, _ranks in _REGISTRY)


def actions_for(
    status: str,
    *,
    verifying: tuple[str, ...] = (),
    pending_proposals: int = 0,
    selected_task: str | None = None,
    holding: bool = False,
) -> list[Action]:
    """Actions that apply to the mission state, most relevant first.

    A task awaiting approval puts `Approve task N` at the top. Terminal missions
    keep only read-only actions. `Proposals` is listed first among the rest only
    when the agent has suggestions waiting.
    """
    state = status.upper()
    ranked: list[tuple[float, int, Action]] = []
    for index, (action, ranks) in enumerate(_REGISTRY):
        rank = ranks.get(state)
        if rank is None:
            continue
        if action.command == "proposal list" and pending_proposals:
            rank = -0.5
        if action.handler == "amend" and selected_task is not None:
            action = replace(action, label=f"Amend task {selected_task}")
        ranked.append((rank, index, action))
    if state not in ENDED_MISSION_STATUSES:
        for number, task_id in enumerate(verifying):
            approve = Action(f"approve {task_id}", f"Approve task {task_id}",
                             "Accept the finished work (asks first)", mutable=True, slash=f"/approve {task_id}")
            ranked.append((-1 + number / 10, -1, approve))
            reject = Action(f"reject {task_id} ", f"Reject task {task_id}",
                            "Send it back; you add the reason", mutable=True, slash=f"/reject {task_id} REASON", handler="prefill")
            ranked.append((-0.95 + number / 10, -1, reject))
        if holding and verifying:
            ranked.append((-0.94 + len(verifying) / 10, -1, Action(
                "continue", "Continue", "Start the next task now; the reviewed task keeps awaiting your decision",
                mutable=True, slash="/continue",
            )))
    return [action for _rank, _index, action in sorted(ranked, key=lambda item: (item[0], item[1]))]
