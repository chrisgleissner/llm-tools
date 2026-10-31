"""Names that Bridge's store, runtime, views and tests share: statuses, event kinds, views and colour roles.

Each class is a ``StrEnum``, so a member compares and hashes equal to its plain string and
``str(member)`` is the plain string. SQL parameters, JSON output and dictionary keys therefore
stay unchanged when a member is used where a literal was. A misspelt member fails at import or
attribute lookup, where a misspelt literal would silently never match.

This module imports only ``enum`` so that the store, the command-line parser and the views can
all use it without loading a heavier module. ``bridge_states`` builds its named status sets and
transition tables from these classes; ``bridge_events`` keys the event registry by ``EventKind``.
"""

from __future__ import annotations

from enum import StrEnum


class MissionStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskStatus(StrEnum):
    QUEUED = "QUEUED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OperationStatus(StrEnum):
    INTENT = "INTENT"
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    EXITED = "EXITED"
    UNCERTAIN = "UNCERTAIN"


class ViewName(StrEnum):
    """The console views in F1-F8 order."""

    OVERVIEW = "Overview"
    TASKS = "Tasks"
    PROVIDERS = "Providers"
    ACTIVITY = "Activity"
    SESSIONS = "Sessions"
    HEALTH = "Health"
    ACTIONS = "Actions"
    USAGE = "Usage"


class ColorRole(StrEnum):
    """Semantic colour roles, in the order that numbers their curses colour pairs."""

    BRAND = "brand"
    INFO = "info"
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    DIM = "dim"
    DIFF_ADD = "diff_add"
    DIFF_REMOVE = "diff_remove"
    DIFF_HUNK = "diff_hunk"
    COMMAND = "command"
    TOOL = "tool"
    STDERR = "stderr"
    HEADING = "heading"
    ACCENT = "accent"
    MUTED = "muted"
    KEY = "key"
    SELECTED = "selected"
    STATE_RUNNING = "state_running"
    STATE_WAITING = "state_waiting"
    STATE_REVIEW = "state_review"
    STATE_DONE = "state_done"
    STATE_FAILED = "state_failed"
    STATE_PAUSED = "state_paused"
    CAPACITY_OK = "capacity_ok"
    CAPACITY_LOW = "capacity_low"
    CAPACITY_EMPTY = "capacity_empty"
    PROVIDER_CODEX = "provider_codex"
    PROVIDER_CLAUDE = "provider_claude"
    PROVIDER_COPILOT = "provider_copilot"
    PROVIDER_OTHER = "provider_other"
    MISSION_1 = "mission_1"
    MISSION_2 = "mission_2"
    MISSION_3 = "mission_3"
    MISSION_4 = "mission_4"
    MISSION_5 = "mission_5"
    MISSION_6 = "mission_6"
    MISSION_7 = "mission_7"
    MISSION_8 = "mission_8"


class EventKind(StrEnum):
    """Every kind of durable event Bridge writes or reads.

    ``bridge_events.EVENTS`` holds one entry per member; a test checks that every kind passed to the
    store's event writers in the Bridge modules is a member.
    """

    CAPACITY_REFRESH = "capacity-refresh"
    CAPACITY_REFRESH_ERROR = "capacity-refresh-error"
    COMMAND_APPLIED = "command-applied"
    COMMAND_FAILED = "command-failed"
    COMMAND_QUEUED = "command-queued"
    COMMAND_REJECTED = "command-rejected"
    CONTROLLER_CONFIG = "controller-config"
    CONTROLLER_EXITED_UNEXPECTEDLY = "controller-exited-unexpectedly"
    CONTROLLER_FAILED = "controller-failed"
    CONTROLLER_HEARTBEAT = "controller-heartbeat"
    CONTROLLER_HEARTBEAT_ERROR = "controller-heartbeat-error"
    CONTROLLER_MODE = "controller-mode"
    CONTROLLER_OWNERSHIP = "controller-ownership"
    DEPENDENCY_ADDED = "dependency-added"
    EVENTS_PRUNED = "events-pruned"
    MANAGED_PROCESS_EXITED = "managed-process-exited"
    MANAGED_PROCESS_PREPARED = "managed-process-prepared"
    MANAGED_PROCESS_STARTED = "managed-process-started"
    MISSION_AUTO_CONTINUE = "mission-auto-continue"
    MISSION_COLOUR = "mission-colour"
    MISSION_CREATED = "mission-created"
    MISSION_REOPENED = "mission-reopened"
    MISSION_STATUS = "mission-status"
    NATIVE_SESSION_RESTORATION = "native-session-restoration"
    OPERATION_ABORTED_BEFORE_LAUNCH = "operation-aborted-before-launch"
    OPERATION_EXITED = "operation-exited"
    OPERATION_LOG_PRUNED = "operation-log-pruned"
    OPERATION_PREPARED = "operation-prepared"
    OPERATION_PROCESS = "operation-process"
    OPERATION_PROGRESS = "operation-progress"
    OPERATION_RUNNING = "operation-running"
    OPERATION_UNCERTAIN = "operation-uncertain"
    PROVIDER_DECLINED = "provider-declined"
    PROVIDER_HANDOFF = "provider-handoff"
    PROVIDER_HANDOFF_EXHAUSTED = "provider-handoff-exhausted"
    PROVIDER_HANDOFF_SELECTED = "provider-handoff-selected"
    PROVIDER_SELECTION = "provider-selection"
    PROVIDER_SWITCHED = "provider-switched"
    PROVIDER_WAIT = "provider-wait"
    REPOSITORY_CHECKPOINT = "repository-checkpoint"
    REVIEW_HOLD = "review-hold"
    REVIEW_HOLD_RELEASED = "review-hold-released"
    ROTATION_APPLIED = "rotation-applied"
    ROTATION_CHANGED = "rotation-changed"
    ROTATION_REJECTED = "rotation-rejected"
    STATION_HANDOFF = "station-handoff"
    STATION_OBSERVATION = "station-observation"
    STATION_STOPPED = "station-stopped"
    STATION_VIEWED = "station-viewed"
    STEERING_HANDOFF_CONSUMED = "steering-handoff-consumed"
    STEERING_HANDOFF_REQUESTED = "steering-handoff-requested"
    STEERING_PAUSE_APPLIED = "steering-pause-applied"
    STEERING_PAUSE_REQUESTED = "steering-pause-requested"
    STEERING_STOP_TASK_APPLIED = "steering-stop-task-applied"
    STEERING_STOP_TASK_REQUESTED = "steering-stop-task-requested"
    STEERING_VERIFY_APPLIED = "steering-verify-applied"
    STEERING_VERIFY_DRAINED = "steering-verify-drained"
    STEERING_VERIFY_REQUESTED = "steering-verify-requested"
    TASK_ADDED = "task-added"
    TASK_AMENDED = "task-amended"
    TASK_AMENDMENT_APPLIED = "task-amendment-applied"
    TASK_DECLINED_RELEASED = "task-declined-released"
    TASK_EVIDENCE_RECORDED = "task-evidence-recorded"
    TASK_LAUNCH_REFUSED = "task-launch-refused"
    TASK_PRIORITY = "task-priority"
    TASK_PROPOSAL_ACCEPTED = "task-proposal-accepted"
    TASK_PROPOSAL_REJECTED = "task-proposal-rejected"
    TASK_PROPOSED = "task-proposed"
    TASK_PROVIDER_CONTINUATION = "task-provider-continuation"
    TASK_READY = "task-ready"
    TASK_REJECTED = "task-rejected"
    TASK_REORDERED = "task-reordered"
    TASK_STATUS = "task-status"
    TASK_VERIFIED = "task-verified"
    VERIFICATION_FAILED = "verification-failed"
