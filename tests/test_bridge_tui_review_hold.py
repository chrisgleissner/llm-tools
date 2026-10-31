"""Console wiring for the native review hold: Needs-you row, status strip, Actions, completion and events."""

from __future__ import annotations

from llm_tools import bridge_tui
from llm_tools.bridge_actions import actions_for

HOLD = {"reason": "review", "task_ordinal": 1, "tasks": [1], "since": "2026-10-29T12:00:00+00:00"}


def holding_snapshot(**extra: object) -> dict[str, object]:
    return {
        "mission": {"id": "M", "status": "RUNNING"},
        "tasks": [
            {"id": 11, "ordinal": 1, "status": "VERIFYING", "title": "first"},
            {"id": 12, "ordinal": 2, "status": "READY", "title": "second"},
        ],
        "hold": HOLD,
        **extra,
    }


def test_needs_you_row_names_the_review_commands_while_holding() -> None:
    rows = bridge_tui._needs_you_rows(holding_snapshot())
    assert rows == ["task 1 awaits your review · /approve 1 · /reject 1 reason · /continue"]


def test_needs_you_row_is_unchanged_without_a_hold_and_for_other_verifying_tasks() -> None:
    snapshot = holding_snapshot(hold=None)
    assert bridge_tui._needs_you_rows(snapshot) == ["task 1 awaits your approval — type /approve 1 (or F7 Actions)"]
    snapshot = holding_snapshot()
    snapshot["tasks"].append({"id": 13, "ordinal": 3, "status": "VERIFYING", "title": "third"})  # type: ignore[union-attr]
    assert bridge_tui._needs_you_rows(snapshot) == [
        "task 1 awaits your review · /approve 1 · /reject 1 reason · /continue",
        "task 3 awaits your approval — type /approve 3 (or F7 Actions)",
    ]


def test_status_strip_shows_the_review_hold_and_degrades_to_fit() -> None:
    text, role = bridge_tui._status_strip(holding_snapshot(), 0)
    assert text == "task 1 awaits your review · /approve 1 · /reject 1 reason · /continue" and role == "warn"
    assert bridge_tui._status_strip(holding_snapshot(), 50)[0] == "task 1 awaits your review · /approve 1 · /continue"
    assert bridge_tui._status_strip(holding_snapshot(), 37)[0] == "task 1 awaits your review · /continue"
    plain = bridge_tui._status_strip(holding_snapshot(hold=None), 0)[0]
    assert plain == "task 1 awaits approval · type /approve 1"


def test_review_words_are_coloured_like_the_approval_phrase() -> None:
    segments = bridge_tui._plain_words("task 1 awaits your review · /continue", "info", "strip")
    assert ("awaits your review", "state_review") in segments


def test_continue_is_an_action_only_while_holding() -> None:
    held = [action.typed for action in actions_for("RUNNING", verifying=("1",), holding=True)]
    assert held[:3] == ["/approve 1", "/reject 1 REASON", "/continue"]
    assert "/continue" not in [action.typed for action in actions_for("RUNNING", verifying=("1",))]
    assert "/continue" not in [action.typed for action in actions_for("RUNNING", holding=True)]
    assert "/continue" not in [action.typed for action in actions_for("COMPLETED", verifying=("1",), holding=True)]
    many = [action.typed for action in actions_for("RUNNING", verifying=("1", "2"), holding=True)]
    assert many.index("/continue") > many.index("/reject 2 REASON")


def test_console_palette_lists_continue_from_the_snapshot_hold() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: {}, full=True)
    state.snapshot = holding_snapshot()
    assert "/continue" in [action.typed for action in state.palette_actions()]
    state.snapshot = holding_snapshot(hold=None)
    assert "/continue" not in [action.typed for action in state.palette_actions()]


def test_continue_completes_as_a_slash_command() -> None:
    assert "/continue" in bridge_tui._command_completions("/con")
    assert bridge_tui._command_completions("/continue")[0] == "/continue"


def test_hold_events_use_their_own_summary_and_are_not_bookkeeping() -> None:
    held = {"kind": "review-hold", "task_id": 11, "data": {"task_ordinal": 1, "summary": "holding the next task · task 1 awaits your review"}}
    released = {"kind": "review-hold-released", "task_id": 11, "data": {"task_ordinal": 1, "how": "continue", "summary": "hold released · continued"}}
    assert bridge_tui._event_summary(held) == "holding the next task · task 1 awaits your review"
    assert bridge_tui._event_summary(released) == "hold released · continued"
    assert bridge_tui._humanize_event(held) == ("Holding the next task · task 1 awaits your review", False)
    assert bridge_tui._humanize_event(released) == ("Hold released · continued", False)
    assert bridge_tui._humanize_event({"kind": "review-hold", "data": {}}) == ("Holding the next task for your review", False)
    assert bridge_tui._humanize_event({"kind": "review-hold-released", "data": {}}) == ("Review hold released", False)
    entries = bridge_tui._activity_entries([{**held, "created_at": "2026-10-29T12:00:00+00:00"}])
    assert [entry.plumbing for entry in entries] == [False]
