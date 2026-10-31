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
    K.OPERATION_PROCESS: _HIDDEN,
    K.REPOSITORY_CHECKPOINT: _HIDDEN,
    K.CONTROLLER_MODE: _HIDDEN,
    K.CONTROLLER_CONFIG: _HIDDEN,
    K.COMMAND_QUEUED: _HIDDEN,
    K.COMMAND_APPLIED: _HIDDEN,
    K.OPERATION_PROGRESS: _HIDDEN,
    K.EVENTS_PRUNED: _HIDDEN,
    K.OPERATION_PREPARED: _PLUMBING,
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
