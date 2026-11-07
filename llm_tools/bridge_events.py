"""Registry of Bridge's durable event kinds and how each is classified.

One ``EventSpec`` per ``EventKind`` replaces the separate sets that the console, the Activity view
and the status projection each kept. The registry holds only classification flags and imports only
``bridge_vocab``, so the store, the monitor and the views can all read it. The operator-facing
phrase for a kind is produced by the humaniser table in ``bridge_tui`` (``_HUMANIZERS``); a test
checks that every humaniser, every kind the Bridge modules write and every registry entry use
members of ``EventKind``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bridge_vocab import EventKind as K


@dataclass(frozen=True)
class EventSpec:
    """Classification of one event kind.

    ``plumbing``: bookkeeping the controller writes for itself. Activity dims these rows and Ctrl-T
    can hide them.
    ``collapsed``: a run of consecutive events of the kind shows as one Activity row.
    ``recent_hidden``: the status projection's recent-events list leaves the kind out.
    """

    plumbing: bool = False
    collapsed: bool = False
    recent_hidden: bool = False


_PLUMBING = EventSpec(plumbing=True)
_RUN = EventSpec(plumbing=True, collapsed=True)  # a heartbeat or refresh every few seconds
_HIDDEN = EventSpec(plumbing=True, recent_hidden=True)

EVENTS: dict[str, EventSpec] = {str(kind): EventSpec() for kind in K}
EVENTS.update({str(kind): spec for kind, spec in {
    K.CONTROLLER_HEARTBEAT: EventSpec(plumbing=True, collapsed=True, recent_hidden=True),
    K.CAPACITY_REFRESH: EventSpec(plumbing=True, collapsed=True, recent_hidden=True),
    K.CONTROLLER_HEARTBEAT_ERROR: _RUN,
    K.CAPACITY_REFRESH_ERROR: _RUN,
    K.CAPACITY_WAKE_PLAN: _PLUMBING,
    K.OPERATION_PROCESS: _HIDDEN,
    K.REPOSITORY_CHECKPOINT: _HIDDEN,
    K.PROMPT_ASSEMBLED: _HIDDEN,
    K.CONTROLLER_MODE: _HIDDEN,
    K.CONTROLLER_CONFIG: _HIDDEN,
    K.COMMAND_QUEUED: _HIDDEN,
    K.COMMAND_APPLIED: _HIDDEN,
    K.OPERATION_PROGRESS: _HIDDEN,
    K.PROVIDER_RESULT_DIAGNOSED: _HIDDEN,
    K.TASK_STATUS_TRANSITION: _PLUMBING,
    K.EVENTS_PRUNED: _HIDDEN,
    K.OPERATION_PREPARED: _PLUMBING,
    K.SUSPEND_DECISION: _PLUMBING,
    K.SUSPEND_PROVIDER_STARTED: _HIDDEN,
    K.OPERATION_LOG_PRUNED: _PLUMBING,
    K.MANAGED_PROCESS_PREPARED: _PLUMBING,
    K.CONTROLLER_OWNERSHIP: _PLUMBING,
    K.PROVIDER_SELECTION: _PLUMBING,
    K.STATION_OBSERVATION: _PLUMBING,
    K.STATION_STOPPED: _PLUMBING,
    K.NATIVE_SESSION_RESTORATION: _PLUMBING,
    K.TASK_PROVIDER_CONTINUATION: _PLUMBING,
    # Steering requests are operator actions; their application is bookkeeping.
    K.STEERING_HANDOFF_CONSUMED: _PLUMBING,
    K.STEERING_PAUSE_APPLIED: _PLUMBING,
    K.STEERING_STOP_TASK_APPLIED: _PLUMBING,
    K.STEERING_VERIFY_APPLIED: _PLUMBING,
    K.STEERING_VERIFY_DRAINED: _PLUMBING,
}.items()})

# Kinds a stored event may carry that are not in ``EventKind``. ``after-verification-result`` was listed as
# bookkeeping by earlier builds although no code writes it; it stays bookkeeping if an old database holds one.
_LEGACY_PLUMBING_KINDS = frozenset({"after-verification-result"})
_PLUMBING_STEERING_SUFFIXES = frozenset({"applied", "drained", "consumed"})


def is_plumbing(kind: str) -> bool:
    """Whether Activity treats events of ``kind`` as bookkeeping (unknown ``steering-*`` kinds by their suffix)."""
    spec = EVENTS.get(kind)
    if spec is not None:
        return spec.plumbing
    if kind in _LEGACY_PLUMBING_KINDS:
        return True
    return kind.startswith("steering-") and kind.rsplit("-", 1)[-1] in _PLUMBING_STEERING_SUFFIXES


def collapsed_kinds() -> frozenset[str]:
    return frozenset(kind for kind, spec in EVENTS.items() if spec.collapsed)


def recent_hidden_kinds() -> frozenset[str]:
    return frozenset(kind for kind, spec in EVENTS.items() if spec.recent_hidden)


def plumbing_kinds() -> frozenset[str]:
    return frozenset(kind for kind, spec in EVENTS.items() if spec.plumbing)


# Retention classes (see ``bridge_retention`` and the "Data retention" section of the operations guide).
#
# ``AUDIT_SUMMARY_KINDS`` is the permanent record of a mission: it is never deleted by retention.
# ``AUDIT_RECORD_KINDS`` is what the operator or the controller decided or did (task edits, operation
# outcomes, handoffs, steering requests, suspend decisions): it is kept while a mission is not finished and
# for ``finished_missions_days`` after it finishes. Every other kind is ``diagnostic`` bookkeeping that
# only explains how the controller got there; ``events_days`` deletes it from finished missions.
# A kind that is not in ``EventKind`` (an old database may hold one) is treated as an audit record.
AUDIT_SUMMARY_KINDS = frozenset(str(kind) for kind in (
    K.MISSION_CREATED, K.MISSION_STATUS, K.MISSION_REOPENED, K.TASK_VERIFIED, K.VERIFICATION_FAILED,
    K.EVENTS_PRUNED, K.OPERATION_LOG_PRUNED, K.CONTROLLER_FAILED, K.CONTROLLER_EXITED_UNEXPECTEDLY,
))
AUDIT_RECORD_KINDS = frozenset(str(kind) for kind in (
    K.TASK_ADDED, K.TASK_AMENDED, K.TASK_AMENDMENT_APPLIED, K.TASK_DECLINED_RELEASED, K.TASK_INTERRUPTED_RELEASED,
    K.TASK_EVIDENCE_RECORDED, K.TASK_LAUNCH_REFUSED, K.TASK_PRIORITY, K.TASK_PROPOSAL_ACCEPTED, K.TASK_PROPOSAL_REJECTED,
    K.TASK_PROPOSED, K.TASK_AWAITING_ANSWER, K.TASK_REJECTED, K.TASK_REORDERED, K.TASK_STATUS,
    K.COMMAND_FAILED, K.COMMAND_REJECTED, K.DEPENDENCY_ADDED, K.MISSION_AUTO_CONTINUE, K.MISSION_POLICY_CHANGED,
    K.MISSION_SUSPEND_WHEN_RATE_LIMITED, K.MISSION_WAITING_FOR_OPERATOR, K.MANAGED_PROCESS_EXITED,
    K.OPERATION_ABORTED_BEFORE_LAUNCH, K.OPERATION_EXITED, K.OPERATION_UNCERTAIN, K.PROVIDER_DECLINED,
    K.PROVIDER_HANDOFF, K.PROVIDER_HANDOFF_EXHAUSTED, K.PROVIDER_HANDOFF_SELECTED, K.PROVIDER_ORPHANED,
    K.PROVIDER_SWITCHED, K.PROVIDER_USAGE_LIMIT, K.REVIEW_HOLD, K.REVIEW_HOLD_RELEASED, K.ROTATION_APPLIED,
    K.ROTATION_CHANGED, K.ROTATION_REJECTED, K.STATION_HANDOFF, K.STEERING_HANDOFF_REQUESTED,
    K.STEERING_PAUSE_REQUESTED, K.STEERING_STOP_TASK_REQUESTED, K.STEERING_VERIFY_REQUESTED, K.SUSPEND_INTENT,
    K.SUSPEND_OUTCOME, K.SUSPEND_REFUSED, K.SUSPEND_WORK_RESUMED, K.SUSPEND_DECISION,
))
DIAGNOSTIC_KINDS = frozenset(str(kind) for kind in K) - AUDIT_SUMMARY_KINDS - AUDIT_RECORD_KINDS


def retention_class(kind: str) -> str:
    """``"summary"``, ``"audit"`` or ``"diagnostic"``: how long retention keeps events of ``kind``."""
    if kind in AUDIT_SUMMARY_KINDS:
        return "summary"
    return "diagnostic" if kind in DIAGNOSTIC_KINDS else "audit"
