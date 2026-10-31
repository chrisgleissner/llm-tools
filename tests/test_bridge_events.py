"""Contract tests for the Bridge event registry: every event kind that is written is classified and phrased."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from llm_tools import bridge_events, bridge_monitor, bridge_tui
from llm_tools.bridge_vocab import EventKind

PACKAGE = Path(bridge_events.__file__).resolve().parent
# Calls whose first or second positional argument (or ``event_kind=`` keyword) is an event kind.
_WRITERS = {"_event", "record_runtime_event", "_record"}


def _literal_kinds() -> dict[str, set[str]]:
    """Event-kind string literals passed to the event writers in the Bridge modules, with where they are."""
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.glob("bridge*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
            candidates: list[ast.expr] = []
            if name in _WRITERS:
                candidates.extend(node.args[:2])
            candidates.extend(keyword.value for keyword in node.keywords if keyword.arg == "event_kind")
            for candidate in candidates:
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str) and "-" in candidate.value:
                    found.setdefault(candidate.value, set()).add(f"{path.name}:{node.lineno}")
    return found


def test_every_kind_the_bridge_modules_write_is_in_the_registry() -> None:
    written = _literal_kinds()
    assert len(written) > 40, "the scan should find the store, runtime and CLI writers"
    unregistered = {kind: sorted(where) for kind, where in written.items() if kind not in bridge_events.EVENTS}
    assert not unregistered, f"add these kinds to bridge_vocab.EventKind and bridge_events.EVENTS: {unregistered}"


def test_kinds_built_at_run_time_are_in_the_registry() -> None:
    # finish_command writes "command-" + status.lower() for these statuses.
    for status in ("applied", "rejected", "failed"):
        assert f"command-{status}" in bridge_events.EVENTS
    # The steering requests and their applications written by bridge.py and the controller.
    for verb in ("pause", "stop-task", "verify", "handoff"):
        assert f"steering-{verb}-requested" in bridge_events.EVENTS


def test_registry_and_enum_list_the_same_kinds() -> None:
    assert set(bridge_events.EVENTS) == {str(kind) for kind in EventKind}


def test_every_humaniser_is_for_a_known_kind() -> None:
    assert set(bridge_tui._HUMANIZERS) <= set(bridge_events.EVENTS)


@pytest.mark.parametrize("kind", [str(kind) for kind in EventKind])
def test_every_kind_has_a_phrase_and_the_registry_flag(kind: str) -> None:
    text, plumbing = bridge_tui._humanize_event({"kind": kind, "data": {}})
    assert text.strip(), kind
    if kind not in {"task-ready", "task-status"}:  # their flag depends on the new task state
        assert plumbing is bridge_events.EVENTS[kind].plumbing, kind
    assert bridge_events.is_plumbing(kind) is bridge_events.EVENTS[kind].plumbing


def test_task_status_events_are_bookkeeping_only_when_the_task_starts() -> None:
    assert bridge_tui._humanize_event({"kind": "task-status", "data": {"to": "RUNNING"}})[1] is True
    assert bridge_tui._humanize_event({"kind": "task-status", "data": {"to": "FAILED"}})[1] is False


def test_unregistered_kinds_keep_the_prefix_rules() -> None:
    assert bridge_events.is_plumbing("steering-new-applied") and not bridge_events.is_plumbing("steering-new-requested")
    assert bridge_tui._humanize_event({"kind": "command-new", "data": {"command": "x"}}) == ("Command new: x", False)
    assert bridge_tui._humanize_event({"kind": "mystery-kind", "data": {}}) == ("Mystery kind", False)
    assert bridge_events.is_plumbing("after-verification-result")


def test_derived_sets_match_the_registry_and_the_status_projection() -> None:
    assert bridge_monitor.PLUMBING_EVENT_KINDS == bridge_events.recent_hidden_kinds()
    assert bridge_tui._COLLAPSED_EVENTS == bridge_events.collapsed_kinds()
    assert bridge_events.recent_hidden_kinds() <= bridge_events.plumbing_kinds()
    assert bridge_events.collapsed_kinds() <= bridge_events.plumbing_kinds()
    for kind in bridge_events.EVENTS:
        assert type(kind) is str
