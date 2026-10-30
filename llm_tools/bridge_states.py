"""State vocabulary and transition tables for Bridge missions, tasks and operations.

This module has no imports so that the store, the runtime, the command line and
every view can share one definition of each status set. A set that two modules
deliberately define differently is declared here once under a name that says
what it contains, rather than as a local literal in each module.
"""

from __future__ import annotations

TASK_STATES = frozenset({"QUEUED", "READY", "RUNNING", "PAUSED", "BLOCKED", "VERIFYING", "COMPLETED", "FAILED", "CANCELLED"})
MISSION_STATES = frozenset({"CREATED", "RUNNING", "PAUSED", "STOPPING", "STOPPED", "COMPLETED", "FAILED", "CANCELLED"})
OPERATION_STATES = frozenset({"INTENT", "PREPARED", "RUNNING", "EXITED", "UNCERTAIN"})

# Exit status Bridge records when the operator declined the provider's own trust prompt
# (llm_tools.common.OPERATOR_DECLINED_STATUS; this module imports nothing, so it is repeated).
OPERATOR_DECLINED_EXIT_CODE = 76

# An operation that has been recorded but has not yet exited or been marked uncertain.
ACTIVE_OPERATION_STATUSES = frozenset({"INTENT", "PREPARED", "RUNNING"})

# Mission statuses that cannot be left, and the same plus FAILED (a failed mission can still be resumed).
FINISHED_MISSION_STATUSES = frozenset({"COMPLETED", "CANCELLED"})
ENDED_MISSION_STATUSES = frozenset({"COMPLETED", "CANCELLED", "FAILED"})
# Statuses in which Bridge closes an idle provider station: ended missions and STOPPED.
STATION_CLOSING_MISSION_STATUSES = frozenset({"COMPLETED", "CANCELLED", "FAILED", "STOPPED"})
# A mission that has not finished and is either created or being worked on.
LIVE_MISSION_STATUSES = frozenset({"CREATED", "RUNNING", "PAUSED", "STOPPING"})
# Statuses in which a controller process is expected to exist.
CONTROLLED_MISSION_STATUSES = frozenset({"RUNNING", "PAUSED", "STOPPING"})
# A started mission that is not yet stopping.
STARTED_MISSION_STATUSES = frozenset({"RUNNING", "PAUSED"})
# The operator has asked work to stop, or has stopped it, but the mission is not over.
HALTED_MISSION_STATUSES = frozenset({"PAUSED", "STOPPING", "STOPPED"})

FINISHED_TASK_STATUSES = frozenset({"COMPLETED", "CANCELLED"})

# Current status -> statuses it may move to. A transition to the same status is
# always accepted by the store and is not listed.
MISSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "CREATED": frozenset({"RUNNING", "CANCELLED"}),
    "RUNNING": frozenset({"PAUSED", "STOPPING", "STOPPED", "COMPLETED", "FAILED", "CANCELLED"}),
    "PAUSED": frozenset({"RUNNING", "STOPPING", "CANCELLED"}),
    "STOPPING": frozenset({"STOPPED", "RUNNING", "FAILED", "CANCELLED"}),
    "STOPPED": frozenset({"RUNNING", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset({"RUNNING", "CANCELLED"}),
    "CANCELLED": frozenset(),
}

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "QUEUED": frozenset({"READY", "PAUSED", "BLOCKED", "CANCELLED"}),
    "READY": frozenset({"QUEUED", "RUNNING", "PAUSED", "BLOCKED", "CANCELLED"}),
    "RUNNING": frozenset({"PAUSED", "BLOCKED", "CANCELLED"}),
    "PAUSED": frozenset({"QUEUED", "READY", "CANCELLED"}),
    "BLOCKED": frozenset({"QUEUED", "READY", "CANCELLED"}),
    "VERIFYING": frozenset({"FAILED", "CANCELLED"}),
    "COMPLETED": frozenset(),
    "FAILED": frozenset({"QUEUED", "CANCELLED"}),
    "CANCELLED": frozenset(),
}

# Vocabulary the command-line parser needs to build its help and choices. It lives here, with no imports,
# so parsing a command does not load the theme module; ``bridge_theme`` re-exports these names.
THEME_NAMES: tuple[str, ...] = ("dark", "light", "high-contrast", "neutral", "mono")
THEME_CHOICES: tuple[str, ...] = ("auto", *THEME_NAMES)
MISSION_COUNT = 8
# Names of the eight identity hues in number order (the roles' descriptions in the docs).
MISSION_HUE_NAMES: tuple[str, ...] = ("teal", "azure", "blue", "indigo", "violet", "magenta", "pink", "slate")
