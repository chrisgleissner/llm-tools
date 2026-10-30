"""Console fixes from the first-time-user critique: task numbers, commands, messages, tabs, usage, approval."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_help, bridge_tui
from llm_tools.bridge_input import cell_width
from llm_tools.bridge_store import BridgeStore

from .test_bridge_tui import FakeWindow
from .test_bridge_tui_experience import Screen, event, full_snapshot, make_state
from tests.conftest import scaled


@pytest.fixture(autouse=True)
def _quiet_curses(monkeypatch):
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "curs_set", lambda _value: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "2000000000")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)


def settle(state, predicate=None, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + scaled(timeout)
    while time.monotonic() < deadline:
        state.refresh(force=True)
        if not state.command_pending and not state.document_pending and (predicate is None or predicate()):
            return
        time.sleep(0.01)
    assert predicate is None or predicate()


# --- two missions in one state directory: task numbers are per mission --------------------------


def _verifying(store: BridgeStore, mission_id: str, prompt: str) -> int:
    task_id = store.add_task(mission_id, prompt, title=prompt)
    if store.get_mission(mission_id)["status"] == "CREATED":
        store.set_mission_status(mission_id, "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation)
    store.finish_operation(operation, exit_code=0)
    assert store.get_task(task_id)["status"] == "VERIFYING"
    return task_id


@pytest.fixture()
def two_missions(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        first = store.create_mission("first", "objective one", cwd=tmp_path, initial_prompt="mission one task")
        second = store.create_mission("second", "objective two", cwd=tmp_path, initial_prompt="mission two task")
        number_one = _verifying(store, second["id"], "second mission approve me")
        number_two = _verifying(store, second["id"], "second mission reject me")
        ids = [t["id"] for t in store.list_tasks(second["id"])]
        ordinals = [t["ordinal"] for t in store.list_tasks(second["id"])]
    assert ordinals == [1, 2, 3] and ids[0] != 1  # global ids differ from the mission's own numbers
    return first["id"], second["id"], number_one, number_two


def _console(mission_id: str):
    state = bridge_tui._ConsoleState(mission_id, bridge.dispatch_console_command, bridge.console_snapshot, full=True)
    settle(state, lambda: bool(state.snapshot.get("tasks")))
    state.choose_view(bridge_tui.VIEWS.index("Tasks"))
    return state


def test_console_commands_use_the_mission_task_number_not_the_global_id(two_missions, tmp_path, monkeypatch) -> None:
    first, second, global_approve, global_reject = two_missions
    state = _console(second)
    shown = [bridge_tui.task_number(task) for task in bridge_tui._items(state.snapshot["tasks"])]
    assert [str(item) for item in shown] == ["1", "2", "3"]
    assert [str(task["id"]) for task in bridge_tui._items(state.snapshot["tasks"])][0] != "1"

    # View: v reads the full prompt of the second mission's task 1.
    state.selected = [str(bridge_tui.task_number(task)) for task in bridge_tui._items(state.snapshot["tasks"])].index("1")
    state.handle_key("v")
    settle(state, lambda: state.document_viewer is not None)
    assert state.document_viewer is not None and "mission two task" in state.document_viewer.source
    state.close_document()

    # Amend: the editor path addresses `--task 1` and changes the right mission's task.
    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.delenv("VISUAL", raising=False)

    def run_editor(argv, *, check, **_kwargs):
        Path(argv[-1]).write_text("mission two task, amended", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(bridge_tui.subprocess, "run", run_editor)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)
    bridge_tui._edit_task_prompt(FakeWindow(24, 80), state, bridge_tui._selected_task_id(state))  # type: ignore[arg-type]
    settle(state)
    assert state.status_message() == "Task 1 amended (revision 2)"
    with bridge.open_store() as store:
        assert "amended" in store.task_by_ordinal(second, 1)["prompt"]
        assert "amended" not in store.task_by_ordinal(first, 1)["prompt"]

    # Approve: `/approve 2` verifies the mission's task 2, not global task 2 (the first mission's task).
    state._confirm_or_dispatch("/approve 2")
    assert state.confirm_command == "task verify --task 2 --approve"
    assert any("Task 2: second mission approve me" in line for line in state.output)
    state.handle_key("y")
    settle(state)
    with bridge.open_store() as store:
        assert store.task_by_ordinal(second, 2)["status"] == "COMPLETED"
        assert store.task_by_ordinal(first, 1)["status"] != "COMPLETED"

    # Reject, with an apostrophe in the free-text reason.
    state._confirm_or_dispatch("/reject 3 it's not done")
    assert state.confirm_command is not None and state.confirm_command.startswith("task reject --task 3 ")
    state.handle_key("y")
    settle(state)
    with bridge.open_store() as store:
        task = store.task_by_ordinal(second, 3)
        assert task["status"] == "QUEUED"


def test_tasks_without_ordinals_are_addressed_by_global_id() -> None:
    snapshot = {"tasks": [{"id": 42, "title": "old", "status": "VERIFYING"}]}
    assert bridge_tui._task_ref(snapshot, "42") == ["--task-id", "42"]
    assert bridge_tui._task_ref({"tasks": [{"id": 42, "ordinal": 2}]}, "2") == ["--task", "2"]
    command, problem = bridge_tui._expand_console_alias("/approve", snapshot)
    assert problem is None and command == "task verify --task-id 42 --approve"
    command, _ = bridge_tui._expand_console_alias("/reject 42 not done", snapshot)
    assert command.startswith("task reject --task-id 42 ")


def test_reject_reason_keeps_apostrophes_literally() -> None:
    command, problem = bridge_tui._expand_console_alias("/reject 2 it's \"not\" done", {"tasks": [{"id": 9, "ordinal": 2, "status": "VERIFYING"}]})
    assert problem is None
    assert bridge_tui._confirmation_reason(command) == "Send task 2 back for another attempt? Reason: it's \"not\" done"


# --- 1/2/3: Actions slash, /home, messages ----------------------------------------------------


def test_slash_then_letter_in_actions_is_a_command_but_slash_alone_filters() -> None:
    state = make_state(full_snapshot(), view="Actions")
    state.handle_key("/")
    assert state.palette_filter_editing
    for char in "reject 3 not done":
        state.handle_key(char)
    assert not state.palette_filter_editing and state.input_focused
    assert state.command == "/reject 3 not done" and state.palette_filter == ""
    # `/` followed by something else still filters; Ctrl-K letters filter too.
    other = make_state(full_snapshot(), view="Actions")
    other.handle_key("/")
    other.handle_key("2")
    assert other.palette_filter_editing and other.palette_filter == "2"
    quick = make_state(full_snapshot(), view="Actions")
    quick.handle_key("\x0b")
    quick.handle_key("p")
    assert quick.palette_filter == "p" and quick.command == ""


def test_home_alias_opens_the_mission_picker() -> None:
    sent: list[str] = []
    state = make_state(full_snapshot())
    state.dispatch = lambda _mission, command: sent.append(command) or bridge_tui.TerminalAction("M1", None, "pick")
    assert state._dispatch("/home")
    settle(state)
    assert sent == ["missions"]
    assert isinstance(state.terminal_action, bridge_tui.TerminalAction) and state.terminal_action.action == "pick"
    assert "home" in bridge_tui._command_completions("ho") or "home" in bridge_tui._CONSOLE_ALIASES


def test_applied_messages_never_show_private_draft_paths() -> None:
    draft = "/home/u/.local/state/llm-tools/llm-bridge/drafts/task-xyz.md"
    added = json.dumps({"status": "APPLIED", "payload": {"action": "task.add"}, "result": {"id": 17, "ordinal": 4, "revision": 1}})
    assert bridge_tui._command_feedback(f"task add --file {draft}", added) == "Task 4 added"
    amended = json.dumps({"status": "APPLIED", "result": {"task_id": 9, "ordinal": 2, "revision": 3}})
    assert bridge_tui._command_feedback(f"task amend --task 2 --file {draft}", amended) == "Task 2 amended (revision 3)"
    other = json.dumps({"status": "APPLIED", "result": {}})
    text = bridge_tui._command_feedback(f"proposal reject --task 2 --file {draft} --mission abc", other)
    assert draft not in text and "drafts" not in text and "abc" not in text and text.startswith("Applied: proposal reject")
    assert bridge_tui._status_role("Task 4 added") == "ok"


def test_transient_message_expires_and_new_commands_retire_it() -> None:
    clock = [100.0]
    state = make_state(full_snapshot())
    state.status_clock = lambda: clock[0]
    state.output.append("Task 4 added")
    assert state.status_message() == "Task 4 added"
    clock[0] += 7.9
    assert state.status_message() == "Task 4 added"
    clock[0] += 0.2
    assert state.status_message() is None
    state.output.append("Task 5 added")
    assert state.status_message() == "Task 5 added"
    state.dismiss_status()
    assert state.status_message() is None


# --- 4: Tasks selection, filter state and cut text -----------------------------------------------


def test_selection_follows_the_task_when_the_list_changes() -> None:
    snapshot = {"tasks": [{"id": 1, "ordinal": 1, "title": "a", "status": "QUEUED"}, {"id": 2, "ordinal": 2, "title": "b", "status": "QUEUED"}]}
    state = make_state(snapshot, view="Tasks")
    state.selected = 1
    newer = {"tasks": [{"id": 3, "ordinal": 3, "title": "new", "status": "RUNNING"}, *snapshot["tasks"]]}
    state._snapshot_result = (bridge_tui._bounded_snapshot(newer), "")
    state.refresh(force=False, now=0.0)
    assert state.selected_task()["id"] == "2"  # type: ignore[index]


def test_filter_is_visible_and_escape_clears_it() -> None:
    state = make_state(full_snapshot(), view="Tasks")
    state.search_query = "retry"
    summary = bridge_tui._orders_summary(state)
    assert "filter: retry · Esc clears" in summary
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert "filter: retry · Esc clears" in screen.lines[1]
    state.handle_key("\x1b")
    assert state.search_query == "" and "filter:" not in bridge_tui._orders_summary(state)
    # Escape with the command line focused first returns to the body and keeps the filter.
    state.search_query = "retry"
    state.input_focused = True
    state.handle_key("\x1b")
    assert state.search_query == "retry" and not state.input_focused


def test_cut_text_is_marked_in_detail_and_preview() -> None:
    long = "word " * 80
    snapshot = {"tasks": [{"id": 1, "ordinal": 1, "title": long, "status": "READY", "prompt": long * 4, "acceptance": [long], "evidence": [long]}]}
    state = make_state(snapshot, view="Tasks")
    state.task_detail = True
    rows = state.rows()
    assert rows[0].endswith("…")
    assert any(row.strip().startswith("1. ") and row.endswith("…") for row in rows)
    assert rows[-1].endswith("…")
    preview = bridge_tui._order_preview_lines(state.selected_task(), 60, 12)  # type: ignore[arg-type]
    assert any(value.endswith("…") for _label, value, _role in preview)
    assert bridge_tui._cut_text("short", 10) == "short"


# --- 5: contextual help ------------------------------------------------------------------------


@pytest.mark.parametrize("view", bridge_tui.VIEWS)
def test_question_mark_opens_short_help_for_the_current_view(view: str) -> None:
    state = make_state(full_snapshot(), view=view)
    state.handle_key("?")
    viewer = state.document_viewer
    assert viewer is not None and view in viewer.title
    text = viewer.source
    assert len(text.splitlines()) <= 14
    assert "/help keys" in text  # link to the full topic
    assert view in text.splitlines()[0]


def test_console_help_lists_slash_commands_not_shell_commands() -> None:
    state = make_state(full_snapshot())
    state._dispatch("/help")
    text = state.document_viewer.source  # type: ignore[union-attr]
    assert "/approve" in text and "/home" in text and "/start" in text
    assert "llm-bridge task add" in text and text.count("llm-bridge ") <= 3  # a pointer, not the shell grammar
    state._dispatch("/help keys")
    assert "Keys and terminal ownership" in state.document_viewer.source  # type: ignore[union-attr]
    assert "remote" in bridge_help.TOPICS and "window-size latest" in bridge_help.TOPICS["remote"]
    assert "Ctrl-] then d" in bridge_help.TOPICS["remote"]


# --- 6: tab bar -------------------------------------------------------------------------------


@pytest.mark.parametrize("width", [60, 80, 100, 120])
def test_tabs_are_never_glued_and_the_active_one_is_visible(width: int) -> None:
    for index in range(len(bridge_tui.VIEWS)):
        text, entries = bridge_tui._nav_layout(index, width, "  ")
        assert cell_width(text) <= width, (width, index, text)
        assert any(position == index for position, _offset, _label in entries)
        for (_p, offset, label), (_q, next_offset, _next) in zip(entries, entries[1:]):
            assert next_offset - (offset + len(label)) >= 2, (width, text)
        for position, offset, label in entries:
            assert text[offset:offset + len(label)] == label
            assert bridge_tui._nav_index_at(offset + 1, index, width, "  ") == position
    wide, _ = bridge_tui._nav_layout(0, 120, "  ")
    assert "1 Overview" in wide and "8 Usage" in wide  # numbers stay when they fit
    narrow, entries = bridge_tui._nav_layout(7, 60, "  ")
    assert narrow.startswith("< ") and len(entries) < 8  # scrolls with an indicator
    eighty, entries = bridge_tui._nav_layout(6, 80, "  ")
    assert len(entries) == 8 and "Actions]  Usage" in eighty


# --- 7: usage table first ---------------------------------------------------------------------

USAGE_TEXT = """LLM Usage · 18:26

Bars: quota rows █ available · ░ spent
Guidance: 5h rows forecast runout.

Provider   Ready   Scope     Remaining         Guidance              Remaining Time   Resets in
────────   ─────   ───────   ───────────────   ───────────────────   ──────────────   ──────────
Claude     yes     5h         48% █████░░░░░   ! empty in 58m        58m              3h 43m
                   weekly     79% ████████░░   ↓ conserve            23h 32m          6d 17h
"""


def test_usage_view_puts_the_table_first_and_hides_the_legend_behind_a_toggle() -> None:
    snapshot = {"usage": {"status": "ready", "age_seconds": 3, "text": USAGE_TEXT}}
    state = make_state(snapshot, view="Usage")
    state.content_width = 200
    rows = state.rows()
    assert rows[0].startswith("Usage · ready") and rows[1].startswith("Provider")
    assert not any("Bars:" in row for row in rows) and rows[-1] == "l shows the legend"
    state.handle_key("l")
    rows = state.rows()
    assert any(row.startswith("Bars:") for row in rows) and rows.index(next(r for r in rows if r.startswith("Bars:"))) > 4
    assert state.command == "" and not state.input_focused
    state.handle_key("l")
    assert state.usage_legend is False


def test_usage_table_drops_remaining_time_before_panning_at_80_columns() -> None:
    snapshot = {"usage": {"status": "ready", "age_seconds": 3, "text": USAGE_TEXT}}
    state = make_state(snapshot, view="Usage")
    state.content_width = 78
    rows = state.rows()
    assert not any("Remaining Time" in row for row in rows)
    assert all(cell_width(row) <= 78 for row in rows[1:-1]), [cell_width(r) for r in rows]
    header = rows[1]
    assert header.split() == ["Provider", "Ready", "Scope", "Remaining", "Guidance", "Resets", "in"]
    claude = next(row for row in rows if row.startswith("Claude"))
    assert claude.index("3h 43m") > claude.index("empty in 58m")  # columns stay aligned
    wide = make_state(snapshot, view="Usage")
    wide.content_width = 200
    assert any("Remaining Time" in row for row in wide.rows())
    screen = Screen(30, 80)
    state.content_width = 0
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert "Remaining Time" not in "".join(screen.lines) and "Resets in" in "".join(screen.lines)


# --- 8: Providers/Health before any run ---------------------------------------------------------


def _usage_snapshot() -> dict[str, object]:
    return {
        "stations": [{"id": "codex", "name": "codex", "model": "gpt-5", "status": "configured"}],
        "usage": {"status": "ready", "age_seconds": 5, "rows": [
            {"provider": "Codex", "scope": "5h", "remaining_percent": 80.0, "estimated_remaining": None, "reset_epoch": 2_000_000_000 + 3600},
            {"provider": "Codex", "scope": "weekly", "remaining_percent": 21.0, "estimated_remaining": None, "reset_epoch": 2_000_000_000 + 86400},
            {"provider": "Claude", "scope": "5h", "remaining_percent": 48.0, "estimated_remaining": None, "reset_epoch": None},
        ]},
    }


def test_providers_view_shows_usage_numbers_before_any_run() -> None:
    state = make_state(_usage_snapshot(), view="Providers")
    rows = state.rows()
    assert not any("no observation yet" in row or "capacity unavailable" in row for row in rows)
    assert any("5h 80% left" in row for row in rows) and any("weekly 21% left" in row for row in rows)
    assert any("ready" in row for row in rows[2:3])
    assert "Claude" not in "\n".join(rows)  # another provider's numbers stay out of this block


def test_health_view_shows_usage_capacity_before_any_run() -> None:
    state = make_state(_usage_snapshot(), view="Health")
    text = "\n".join(state.rows())
    assert "from llm-usage" in text and "5h 80% left" in text and "5h 48% left" in text
    assert "no capacity observation" not in text


def test_snapshot_reader_adds_usage_for_providers_and_health_only() -> None:
    calls: list[str] = []

    def usage() -> dict[str, object]:
        calls.append("usage")
        return {"status": "ready", "rows": []}

    state = bridge_tui._ConsoleState("M1", lambda *_: "", lambda *_args, **_kw: {"mission": {"id": "M1"}}, full=True)
    state.usage_reader = usage
    state.choose_view(bridge_tui.VIEWS.index("Overview"))
    settle(state, lambda: bool(state.snapshot))
    assert calls == [] and "usage" not in state.snapshot
    state.choose_view(bridge_tui.VIEWS.index("Providers"))
    state.last_refresh = 0.0
    settle(state, lambda: "usage" in state.snapshot)
    assert calls and set(calls) == {"usage"}


# --- 9: approve shows what is approved -----------------------------------------------------------


def test_approve_confirmation_shows_title_evidence_exit_status_and_workspace() -> None:
    snapshot = full_snapshot()
    snapshot["tasks"] = [{"id": 11, "ordinal": 3, "title": "Fix the typo", "status": "VERIFYING",
                          "evidence": ["pytest: 12 passed", "ruff: clean", "manual: README reads well", "extra: latest line"]}]
    snapshot["operations"] = [{"id": "op-1", "task_id": 11, "station": "codex", "status": "EXITED", "exit_code": 0, "created_at": "2033-01-01T00:00:00+00:00"}]
    snapshot["events"] = [event("operation-progress", {"before": "aaa", "after": "bbb"}, task_id=11)]
    state = make_state(snapshot)
    state._confirm_or_dispatch("/approve 3")
    text = "\n".join(state.output)
    assert "Task 3: Fix the typo" in text
    assert "Evidence (latest 3 of 4):" in text and "extra: latest line" in text and "pytest: 12 passed" not in text
    assert "Last run: codex, exit 0, exited" in text
    assert "Workspace: changed during the run" in text
    assert state.output[-1] == "Y approve · N or Esc cancel"
    screen = Screen(20, 100)
    bridge_tui._draw_response_overlay(screen, state)  # type: ignore[arg-type]
    assert "Y approve" in "".join(screen.lines) and "Fix the typo" in "".join(screen.lines)
    # Other confirmations keep their wording.
    state._confirm_or_dispatch("/cancel")
    assert state.output[-1] == "Y confirm · N or Esc cancel"


def test_approve_confirmation_without_evidence_says_so() -> None:
    state = make_state({"tasks": [{"id": 1, "ordinal": 1, "title": "t", "status": "VERIFYING"}], "mission": {"id": "M1", "status": "RUNNING"}})
    state._confirm_or_dispatch("/approve")
    assert any("Evidence: none recorded" in line for line in state.output)
    assert not any(line.startswith(("Last run", "Workspace")) for line in state.output)


# --- 11: attention budget ---------------------------------------------------------------------


def test_historical_failures_and_blocked_needs_you_rows_are_not_bold(monkeypatch) -> None:
    assert bridge_tui.role_attr("error.quiet") & bridge_tui.curses.A_BOLD == 0
    rows = bridge_tui._activity_row_segments("12:00:00  Task 3 verification failed", "error")
    assert ("Task 3 verification failed", "error.quiet") in rows
    segments = bridge_tui._value_segments("Overview", "Needs you", "task 4 BLOCKED · task 5 FAILED — inspect in Tasks")
    assert [role for text, role in segments if text in {"BLOCKED", "FAILED"}] == ["state_failed.quiet", "state_failed.quiet"]
    recent = bridge_tui._value_segments("Overview", "Recent", "12:00:00  Provider exited (1) on task 3")
    assert recent[-1][1] == "error.quiet"
    label = bridge_tui._labelled_segments("Overview", "Needs you   task 4 BLOCKED", bridge_tui._BRIDGE_LABEL, "")[0][0]
    assert label == ("Needs you", "state_review")  # the one bold amber anchor


# --- 12: humanised events ---------------------------------------------------------------------


@pytest.mark.parametrize("row,text", [
    (event("provider-switched", {"from": "codex", "to": "claude"}), "Switched provider codex → claude"),
    (event("station-handoff", {"from": "codex", "to": "claude"}), "Switched provider codex → claude"),
    (event("rotation-changed", {"from": "codex", "to": "claude,codex", "providers": "claude,codex"}), "Rotation changed to claude → codex"),
    (event("rotation-applied", {"providers": "claude,codex", "routes": "", "error": ""}), "Rotation applied: claude → codex"),
    (event("rotation-rejected", {"providers": "x", "error": "unknown provider"}), "Rotation change rejected · unknown provider"),
    (event("task-rejected", {"reason": "tests missing", "revision": 2}, task_id=7), "Task 7 sent back · tests missing"),
    (event("mission-reopened", {"reason": "new task"}), "Mission reopened · new task"),
    (event("controller-failed", {"reason": "crash"}), "Controller failed · crash"),
    (event("mission-colour", {"value": 3}), "Mission colour set to 3 (blue)"),
])
def test_common_events_have_plain_phrases(row, text) -> None:
    assert bridge_tui._humanize_event(row)[0] == text


def test_events_show_the_task_number_of_the_mission() -> None:
    row = event("task-verified", {"approved": True}, task_id=11)
    row["task_ordinal"] = 2
    assert bridge_tui._humanize_event(row)[0] == "Task 2 verified (approved by you)"
    assert bridge_tui._humanize_event(event("task-added", {"title": "x"}, task_id=11), {"11": "4"})[0] == "Task 4 added · x"


def test_evidence_records_show_their_text_everywhere() -> None:
    record = {"at": "2033-01-01T00:00:00+00:00", "revision": 1, "source": "operator", "text": "pytest: 21 passed"}
    assert bridge_tui._evidence_text(record) == "pytest: 21 passed"
    assert bridge_tui._evidence_text(str(record)) == "pytest: 21 passed"
    assert bridge_tui._evidence_text({"revision": 2}) == "revision=2"
    snapshot = {"tasks": [{"id": 1, "ordinal": 1, "title": "t", "status": "VERIFYING", "evidence": [record]}]}
    state = make_state(snapshot, view="Tasks")
    preview = bridge_tui._order_preview_lines(state.selected_task(), 80, 10)  # type: ignore[arg-type]
    assert ("Evidence", "1 item · latest: pytest: 21 passed", "info") in preview
    state.task_detail = True
    assert any("1. pytest: 21 passed" in row for row in state.rows())


def test_evidence_recorded_event_has_a_plain_phrase() -> None:
    row = event("task-evidence-recorded", {"evidence_count": 2}, task_id=3)
    assert bridge_tui._humanize_event(row)[0] == "Task 3 evidence recorded (2 items)"


def test_usage_rows_colour_only_the_status_line_and_header(monkeypatch) -> None:
    snapshot = {"usage": {"status": "ready", "age_seconds": 3, "text": USAGE_TEXT}}
    state = make_state(snapshot, view="Usage")
    screen = Screen(30, 120)
    monkeypatch.setattr(bridge_tui, "role_attr", lambda role: {"heading": 1, "dim": 2, "info": 3, "ok": 4}.get(role, 0))
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    seen = {text.split()[0]: attr for _y, _x, text, attr in screen.writes if text.strip()}
    assert seen["Usage"] == 4 and seen["Provider"] == 1 and seen["────────"] == 2 and seen["Claude"] == 3
