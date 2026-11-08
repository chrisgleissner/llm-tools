"""State vocabulary and transition tables for Bridge missions, tasks and operations.

The status names are the ``StrEnum`` classes of ``bridge_vocab``; this module builds every named
set and transition table from them, as sets of plain strings, so the store, the runtime, the
command line and every view share one definition of each set. A set that two modules
deliberately define differently is declared here once under a name that says what it contains,
rather than as a local literal in each module.

It imports only ``bridge_vocab`` (which imports only ``enum``), so importing it stays cheap.
"""

from __future__ import annotations

from .bridge_vocab import MissionStatus as M, OperationStatus as O, TaskStatus as T


def _names(*members: object) -> frozenset[str]:
    return frozenset(str(member) for member in members)


def _table(rows: dict[T, tuple[T, ...]] | dict[M, tuple[M, ...]]) -> dict[str, frozenset[str]]:
    return {str(source): _names(*targets) for source, targets in rows.items()}


TASK_STATES = _names(*T)
MISSION_STATES = _names(*M)
OPERATION_STATES = _names(*O)

# Exit status Bridge records when the operator declined the provider's own trust prompt
# (llm_tools.common.OPERATOR_DECLINED_STATUS; this module imports nothing, so it is repeated).
OPERATOR_DECLINED_EXIT_CODE = 76

# Exit status of a run that llm-scheduler's own watchdog stopped: no output progress, a question
# that stayed unanswered, or a blocked prompt (llm_tools.common.AUTONOMY_ABORT_STATUS).
AUTONOMY_ABORT_EXIT_CODE = 75

# An operation that has been recorded but has not yet exited or been marked uncertain.
ACTIVE_OPERATION_STATUSES = _names(O.INTENT, O.PREPARED, O.RUNNING)

# Mission statuses that cannot be left, and the same plus FAILED (a failed mission can still be resumed).
FINISHED_MISSION_STATUSES = _names(M.COMPLETED, M.CANCELLED)
ENDED_MISSION_STATUSES = _names(M.COMPLETED, M.CANCELLED, M.FAILED)
# Statuses in which `mission delete` may remove a mission: it never started (CREATED) or it has ended or
# been stopped. RUNNING, PAUSED and STOPPING missions have a controller and are refused.
DELETABLE_MISSION_STATUSES = _names(M.CREATED, M.COMPLETED, M.FAILED, M.CANCELLED, M.STOPPED)
# Statuses in which Bridge closes an idle provider station: ended missions and STOPPED.
STATION_CLOSING_MISSION_STATUSES = _names(M.COMPLETED, M.CANCELLED, M.FAILED, M.STOPPED)
# A mission that has not finished and is either created or being worked on.
LIVE_MISSION_STATUSES = _names(M.CREATED, M.RUNNING, M.PAUSED, M.STOPPING)
# Statuses in which a controller process is expected to exist.
CONTROLLED_MISSION_STATUSES = _names(M.RUNNING, M.PAUSED, M.STOPPING)
# A started mission that is not yet stopping.
STARTED_MISSION_STATUSES = _names(M.RUNNING, M.PAUSED)
# The operator has asked work to stop, or has stopped it, but the mission is not over.
HALTED_MISSION_STATUSES = _names(M.PAUSED, M.STOPPING, M.STOPPED)

FINISHED_TASK_STATUSES = _names(T.COMPLETED, T.CANCELLED)

# A COMPLETED mission that receives a follow-up task goes back to this state (the one in which a mission
# has work but no controller yet), and `start` runs the new task.  `BridgeStore.add_task` is the only
# way out of COMPLETED, so the move is not in MISSION_TRANSITIONS, which stays empty for finished missions.
REOPENED_MISSION_STATUS = M.CREATED.value

# Current status -> statuses it may move to. A transition to the same status is
# always accepted by the store and is not listed.
MISSION_TRANSITIONS: dict[str, frozenset[str]] = _table({
    M.CREATED: (M.RUNNING, M.CANCELLED),
    M.RUNNING: (M.PAUSED, M.STOPPING, M.STOPPED, M.COMPLETED, M.FAILED, M.CANCELLED),
    M.PAUSED: (M.RUNNING, M.STOPPING, M.CANCELLED),
    M.STOPPING: (M.STOPPED, M.RUNNING, M.FAILED, M.CANCELLED),
    M.STOPPED: (M.RUNNING, M.CANCELLED),
    M.COMPLETED: (),
    M.FAILED: (M.RUNNING, M.CANCELLED),
    M.CANCELLED: (),
})

TASK_TRANSITIONS: dict[str, frozenset[str]] = _table({
    T.QUEUED: (T.READY, T.PAUSED, T.BLOCKED, T.CANCELLED),
    T.READY: (T.QUEUED, T.RUNNING, T.PAUSED, T.BLOCKED, T.CANCELLED),
    T.RUNNING: (T.PAUSED, T.BLOCKED, T.CANCELLED),
    T.PAUSED: (T.QUEUED, T.READY, T.CANCELLED),
    T.BLOCKED: (T.QUEUED, T.READY, T.CANCELLED),
    T.VERIFYING: (T.FAILED, T.CANCELLED),
    T.COMPLETED: (),
    T.FAILED: (T.QUEUED, T.CANCELLED),
    T.CANCELLED: (),
})

# Vocabulary the command-line parser needs to build its help and choices. It lives here, with no imports,
# so parsing a command does not load the theme module; ``bridge_theme`` re-exports these names.
THEME_NAMES: tuple[str, ...] = ("dark", "light", "high-contrast", "neutral", "mono")
THEME_CHOICES: tuple[str, ...] = ("auto", *THEME_NAMES)
MISSION_COUNT = 8
# Names of the eight identity hues in number order (the roles' descriptions in the docs).
MISSION_HUE_NAMES: tuple[str, ...] = ("teal", "azure", "blue", "indigo", "violet", "magenta", "pink", "slate")
