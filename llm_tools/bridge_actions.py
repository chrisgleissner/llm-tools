"""Small labelled action registry; argparse remains the command grammar."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    command: str
    label: str
    effect: str
    task: bool = False
    mutable: bool = False


ACTIONS = (
    Action('help', 'Help', 'Read the offline workflow'),
    Action('status', 'Status', 'Inspect current work and blockers'),
    Action('orders', 'Tasks', 'Inspect saved orders'),
    Action('events', 'Events', 'Read recent operational history'),
    Action('pause', 'Pause', 'Hold after the current invocation', mutable=True),
    Action('resume', 'Resume', 'Continue eligible work', mutable=True),
    Action('stop', 'Stop', 'Stop this owned controller'),
    Action('cancel', 'Cancel mission', 'Cancel this mission; confirmation required', mutable=True),
    Action('steer --help', 'Steering help', 'Learn safe boundary steering'),
    Action('attach', 'Open provider', 'Attach the existing owned station'),
    Action('detach', 'Detach', 'Leave work running'),
    Action('usage', 'Usage', 'Inspect provider and route capacity'),
    Action('control', 'Bridge', 'Return to the overview'),
    Action('task add --help', 'Add task', 'Compose a new task', mutable=True),
    Action('proposal list', 'Proposals', 'Inspect pending proposals'),
    Action('start', 'Start', 'Start paid/provider work explicitly', mutable=True),
    Action('task amend --task {task}', 'Amend task', 'Edit the selected task source', task=True, mutable=True),
    Action('task verify --task {task} --approve', 'Approve task', 'Approve reviewed evidence; confirmation required', task=True, mutable=True),
)
