"""Control console overhaul: view names, status strip, humanized activity, Markdown preview, Providers, Actions."""

from __future__ import annotations

import time

import pytest

from llm_tools import bridge_actions, bridge_tui, common
from llm_tools.bridge_input import cell_width
from llm_tools.bridge_tui import ControlViewAction, VIEWS, normalize_view
from llm_tools.terminal import TerminalAction

from .test_bridge_tui_experience import Screen, event, full_snapshot, make_state
from tests.conftest import wait_for as _shared_wait_for

NOW = 2_000_000_000


@pytest.fixture(autouse=True)
def _quiet_curses(monkeypatch):
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "curs_set", lambda _value: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", str(NOW))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("LLM_USAGE_NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


def settle(state) -> None:
    deadline = time.monotonic() + 3
    while state.command_pending and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    state.refresh(force=True)
    assert not state.command_pending


def snapshot_with(**changes) -> dict[str, object]:
    snapshot = full_snapshot()
    snapshot.update(changes)
    return snapshot


# --- 1. view names, order and aliases -------------------------------------------------------

def test_views_use_plain_names_in_documented_f_key_order() -> None:
    assert VIEWS == ("Overview", "Tasks", "Providers", "Activity", "Sessions", "Health", "Actions", "Usage")
    state = make_state()
    for number, name in enumerate(VIEWS, start=1):
        state.handle_key(f"KEY_F{number}")
        assert state.view == name
    nav = bridge_tui._nav_line(0, "  ")
    assert "1 Overview" in nav and "2 Tasks" in nav and "3 Providers" in nav and "4 Activity" in nav


@pytest.mark.parametrize("old,new", [
    ("Bridge", "Overview"), ("Orders", "Tasks"), ("Stations", "Providers"), ("Systems", "Health"),
    ("Log", "Activity"), ("Palette", "Actions"), ("control", "Overview"), ("tasks", "Tasks"),
    ("ACTIVITY", "Activity"), ("usage", "Usage"), ("Sessions", "Sessions"),
])
def test_old_and_new_view_names_normalize(old: str, new: str) -> None:
    assert normalize_view(old) == new
    assert ControlViewAction("m", old).view == new
    assert ControlViewAction("m", old) == ControlViewAction("m", new)
    assert normalize_view("nonsense") == "nonsense"


def test_all_eight_view_names_fit_the_80_column_navigation_in_every_state() -> None:
    for index in range(len(VIEWS)):
        text, entries = bridge_tui._nav_layout(index, 80, "  ")
        assert cell_width(text) <= 80 and len(entries) == len(VIEWS), (index, text)
        assert "<" not in text and ">" not in text  # nothing scrolled off
        selected = next(label for position, _offset, label in entries if position == index)
        assert selected == f"[{VIEWS[index]}]"  # 80 columns: no numbers, two spaces between tabs
        for position, offset, label in entries:
            assert text[offset:offset + len(label)] == label
            assert bridge_tui._nav_index_at(offset + 1, index, 80, "  ") == position


@pytest.mark.parametrize("word,view", [
    ("overview", "Overview"), ("control", "Overview"), ("providers", "Providers"), ("stations", "Providers"),
    ("activity", "Activity"), ("log", "Activity"), ("health", "Health"), ("systems", "Health"),
    ("actions", "Actions"), ("palette", "Actions"), ("sessions", "Sessions"), ("usage", "Usage"),
])
def test_typing_a_view_name_switches_view_without_dispatching(word: str, view: str) -> None:
    sent: list[str] = []
    state = make_state()
    state.dispatch = lambda _mission, command: sent.append(command) or "ok"
    assert state._dispatch(f"/{word}") and state.view == view and sent == []
    assert not state.command_pending and state.expand_requested


def test_orders_and_tasks_commands_still_reach_the_dispatcher() -> None:
    sent: list[str] = []
    state = make_state()
    state.dispatch = lambda _mission, command: sent.append(command) or "ok"
    for word in ("orders", "tasks"):
        state._dispatch(word)
        settle(state)
    assert sent == ["orders", "tasks"] and state.view == "Overview"


def test_view_commands_are_completed_and_control_view_action_selects_normalized_view() -> None:
    assert {"overview", "providers", "activity", "health", "actions"} <= set(bridge_tui._command_completions(""))
    assert bridge_tui._command_completions("/act") == ["/activity", "/actions"]
    state = make_state()
    state._command_result = ControlViewAction("M1", "Orders")
    state.refresh(force=True)
    assert state.view == "Tasks"


# --- 2. status strip ---------------------------------------------------------------------------

def running_snapshot(**changes) -> dict[str, object]:
    snapshot = snapshot_with(
        mission={"id": "M1", "name": "Audit", "status": "RUNNING", "objective": "obj"},
        tasks=[{"id": 6, "title": "Audit", "status": "RUNNING"}, {"id": 7, "title": "Next", "status": "READY"}],
        current_task={"id": 6, "title": "Audit", "status": "RUNNING"},
        stations=[{"id": "codex", "name": "codex", "model": "gpt-5", "observed_model": "unknown"}],
        station_context={"executing_operation": "op-1", "operation_status": "RUNNING", "last_launched_station": "codex"},
        execution={"operation_elapsed_seconds": 250},
        output_idle_seconds=95,
    )
    snapshot["capacity"] = {"selection": {"route": "codex", "decisions": [{"route": "codex", "windows": [
        {"name": "5h", "kind": "reset_window", "remaining_percent": 95.0},
        {"name": "weekly", "kind": "reset_window", "remaining_percent": 21.0},
        {"name": "credits", "kind": "opaque"},
    ]}]}}
    snapshot.update(changes)
    return snapshot


def strip(snapshot: dict[str, object], width: int = 0) -> tuple[str, str]:
    return bridge_tui._status_strip(bridge_tui._bounded_snapshot(snapshot) | {"output_idle_seconds": snapshot.get("output_idle_seconds")}, width)


def test_strip_for_running_task_shows_provider_task_inactivity_and_capacity() -> None:
    text, role = strip(running_snapshot())
    assert text == "codex (gpt-5) · task 6 running 4m · no output 1m · 5h 95% left · week 21% left"
    assert role == "warn"
    quiet, quiet_role = strip(running_snapshot(output_idle_seconds=3))
    assert quiet == "codex (gpt-5) · task 6 running 4m · 5h 95% left · week 21% left" and quiet_role == "ok"


@pytest.mark.parametrize("width,expected", [
    (80, "codex (gpt-5) · task 6 running 4m · no output 1m · 5h 95% left · week 21% left"),
    (72, "codex · task 6 running 4m · no output 1m · 5h 95% left · week 21% left"),
    (60, "codex · task 6 running 4m · no output 1m · 5h 95% · week 21%"),
    (44, "codex · task 6 running 4m · no output 1m"),
    (30, "codex · task 6 running 4m"),
    (12, "codex · tas…"),
])
def test_strip_drops_lowest_value_parts_to_fit(width: int, expected: str) -> None:
    text, _ = strip(running_snapshot(), width)
    assert text == expected and cell_width(text) <= width


def test_strip_without_a_known_owner_or_elapsed_time() -> None:
    snapshot = running_snapshot(station_context={"executing_operation": "none"}, execution={})
    assert strip(snapshot)[0] == "task 6 running · no output 1m"


@pytest.mark.parametrize("status,tasks,expected,role", [
    ("CREATED", [{"id": 1, "status": "READY"}], "not started · next: task 1 · type /start", "dim"),
    ("CREATED", [], "not started · no task waiting · Ctrl-E adds one", "dim"),
    ("STOPPED", [{"id": 2, "status": "QUEUED"}], "stopped · next: task 2 · type /start", "dim"),
    ("PAUSED", [{"id": 3, "status": "READY"}], "paused · next: task 3 · type /resume", "warn"),
    ("STOPPING", [], "stopping after the current step", "warn"),
    ("RUNNING", [{"id": 7, "status": "READY"}], "idle · next: task 7", "dim"),
    ("RUNNING", [], "idle · no task waiting · Ctrl-E adds one", "dim"),
    ("RUNNING", [{"id": 4, "status": "VERIFYING"}], "task 4 awaits approval · type /approve 4", "warn"),
    ("RUNNING", [{"id": 4, "status": "VERIFYING"}, {"id": 5, "status": "VERIFYING"}], "task 4 awaits approval (+1 more) · type /approve 4", "warn"),
    ("COMPLETED", [{"id": 1, "status": "COMPLETED"}, {"id": 2, "status": "COMPLETED"}], "mission completed · 2/2 tasks completed", "ok"),
    ("CANCELLED", [{"id": 1, "status": "CANCELLED"}], "mission cancelled · 0/1 tasks completed", "error"),
    ("FAILED", [], "mission failed", "error"),
])
def test_strip_when_nothing_runs(status: str, tasks: list, expected: str, role: str) -> None:
    assert strip({"mission": {"status": status}, "tasks": tasks}) == (expected, role)


def test_strip_reports_a_capacity_wait_and_loading_state() -> None:
    waiting = {"mission": {"status": "RUNNING"}, "capacity": {"wait": {"target_epoch": NOW + 7200, "reason": "rate-limited"}}}
    assert strip(waiting) == ("waiting for capacity · resumes in 2h", "warn")
    undated = {"mission": {"status": "RUNNING"}, "capacity": {"wait": {"reason": "rate-limited"}}}
    assert strip(undated)[0] == "waiting for capacity"
    assert bridge_tui._status_strip({}) == ("loading…", "dim")


def test_full_view_line_one_is_the_strip_not_the_objective() -> None:
    state = make_state(running_snapshot())
    state.snapshot["output_idle_seconds"] = 95
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[1].startswith("■ codex (gpt-5) · task 6 running 4m · no output 1m")
    assert "Objective" not in screen.lines[1]
    failed = make_state()
    failed.snapshot = {}
    failed.snapshot_error = "boom"
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, failed)  # type: ignore[arg-type]
    assert screen.lines[1].startswith("status unavailable")


def test_tasks_view_keeps_page_summary_only_when_searching_or_paged() -> None:
    state = make_state(running_snapshot(), view="Tasks")
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[1].startswith("■ codex (gpt-5)")
    state.search_query = "audit"
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[1].startswith("Tasks: 2 shown · filter: audit · Esc clears")
    state.search_query = ""
    state.snapshot["task_total"] = 50
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[1].startswith("Tasks: 50 total · showing 1-2")


def test_objective_stays_available_through_o() -> None:
    state = make_state(running_snapshot())
    state.handle_key("o")
    assert state.document_pending  # the full objective is read in the background and opens in the viewer


# --- 3. humanized events, plumbing, heartbeat placement --------------------------------------

@pytest.mark.parametrize("row,text,plumbing", [
    (event("mission-created", {"name": "Demo"}), "Mission created · Demo", False),
    (event("mission-status", {"from": "CREATED", "to": "RUNNING"}), "Mission started", False),
    (event("mission-status", {"from": "PAUSED", "to": "RUNNING"}), "Resumed", False),
    (event("mission-status", {"from": "RUNNING", "to": "PAUSED"}), "Paused", False),
    (event("mission-status", {"from": "RUNNING", "to": "COMPLETED"}), "Mission completed", False),
    (event("mission-status", {"from": "RUNNING", "to": "STOPPED", "reason": "operator"}), "Stopped (operator)", False),
    (event("task-added", {"title": "Do it"}, task_id=7), "Task 7 added · Do it", False),
    (event("task-ready", {"from": "QUEUED", "to": "READY"}, task_id=6), "Task 6 ready", False),
    (event("task-status", {"from": "READY", "to": "RUNNING"}, task_id=6), "Task 6 running", True),
    (event("task-status", {"from": "RUNNING", "to": "VERIFYING"}, task_id=6), "Task 6 awaiting approval", False),
    (event("task-status", {"from": "VERIFYING", "to": "COMPLETED"}, task_id=6), "Task 6 completed", False),
    (event("task-status", {"from": "READY", "to": "QUEUED", "reason": "dependency-added"}, task_id=6), "Task 6 waiting on a new dependency", False),
    (event("operation-running", {"route_id": "codex", "model": "gpt-5", "process_pid": 1}, task_id=6), "Started codex (gpt-5) on task 6", False),
    (event("operation-running", {}), "Started provider", False),
    (event("operation-exited", {"exit_code": 0, "task_status": "VERIFYING"}, task_id=6), "Provider exited (0) on task 6", False),
    (event("operation-uncertain", {"reason": "controller exited"}), "Provider outcome unknown · controller exited", False),
    (event("operation-aborted-before-launch", {"reason": "no capacity"}), "Launch aborted · no capacity", False),
    (event("operation-prepared", {"station": "codex"}, task_id=6), "Preparing codex on task 6", True),
    (event("operation-process", {"process_pid": 575225, "process_start": "e6af"}), "Provider process started", True),
    (event("operation-progress", {}), "Provider progress noted", True),
    (event("provider-selection", {"route": "codex", "selected_model": "gpt-5"}), "Selected codex (gpt-5)", True),
    (event("capacity-refresh", {"provider": "codex", "capacity_brief": "5h 95%, week 21%"}), "Capacity: codex 5h 95%, week 21%", True),
    (event("capacity-refresh", {}), "Capacity refreshed: providers", True),
    (event("capacity-refresh-error", {"reason": "TimeoutError"}), "Capacity refresh failed (TimeoutError)", True),
    (event("controller-heartbeat", {"controller_state": "ACTIVE"}), "controller alive", True),
    (event("controller-heartbeat-error", {"reason": "OSError"}), "controller heartbeat failed (OSError)", True),
    (event("controller-exited-unexpectedly", {"reason": "crash"}), "Controller exited unexpectedly (crash)", False),
    (event("controller-mode", {"actual_mode": "native", "verified": True}), "Controller mode: native", True),
    (event("controller-config", {"values": {"routes": "codex"}}), "Controller configured", True),
    (event("controller-ownership", {"status": "healthy"}), "Controller ownership verified", True),
    (event("repository-checkpoint", {"task_id": 6, "revision": 1}), "Repository checkpoint saved", True),
    (event("station-observation", {"model": "x"}), "Provider screen observed", True),
    (event("command-queued", {"command": "pause"}), "Command queued: pause", True),
    (event("command-applied", {"command": "pause"}), "Command applied: pause", True),
    (event("command-rejected", {"command": "task.verify"}), "Command rejected: task.verify", False),
    (event("native-session-restoration", {"provider": "codex", "restoration_state": "RESTORED_NATIVE"}), "Session restored native · codex", True),
    (event("station-handoff", {"from": "codex", "to": "claude", "reason": "limit"}), "Switched provider codex → claude (limit)", False),
    (event("provider-handoff", {"exit_code": 1, "failure_count": 1, "max_failures": 2}, task_id=6), "Task 6 handed off after exit 1 (failure 1/2)", False),
    (event("provider-handoff-selected", {"station": "claude"}), "Handing off to claude", False),
    (event("provider-handoff-exhausted", {"failures": 3, "max_failures": 2}, task_id=6), "Task 6 out of handoffs (3 failures, limit 2)", False),
    (event("provider-wait", {"target_epoch": NOW + 3600, "reason": "rate-limited"}), "Waiting for capacity, resumes in 1h · rate-limited", False),
    (event("task-verified", {"approved": True}, task_id=6), "Task 6 verified (approved by you)", False),
    (event("task-verified", {}, task_id=6), "Task 6 verified", False),
    (event("verification-failed", {}, task_id=6), "Task 6 verification failed", False),
    (event("task-amended", {}, task_id=6), "Task 6 amended", False),
    (event("task-amendment-applied", {}, task_id=6), "Task 6 amendment applied", False),
    (event("task-priority", {}, task_id=6), "Task 6 priority changed", False),
    (event("task-reordered", {}, task_id=6), "Task 6 reordered", False),
    (event("dependency-added", {"depends_on": [3, 4]}, task_id=6), "Task 6 now waits for 3, 4", False),
    (event("task-proposed", {}, task_id=8), "Agent proposed task 8", False),
    (event("task-proposal-accepted", {}, task_id=8), "Proposal accepted: task 8", False),
    (event("task-proposal-rejected", {}, task_id=8), "Proposal rejected: task 8", False),
    (event("steering-pause-requested", {}), "Steering pause requested", False),
    (event("steering-pause-applied", {}), "Steering pause applied", True),
    (event("something-new", {}), "Something new", False),
    (event("something-new", {"a": 1}), "Something new · a=1", False),
])
def test_events_become_short_verb_phrases_and_are_classified(row, text, plumbing) -> None:
    assert bridge_tui._humanize_event(row) == (text, plumbing)


def test_activity_role_marks_problems_and_waits() -> None:
    assert bridge_tui._activity_role("Provider exited (1) on task 6") == "error"
    assert bridge_tui._activity_role("Provider exited (0) on task 6") == "info"
    assert bridge_tui._activity_role("Task 6 verification failed") == "error"
    assert bridge_tui._activity_role("Task 6 awaiting approval") == "warn"
    assert bridge_tui._activity_role("Task 6 completed") == "info"


def clock(second: int, minute: int = 0) -> str:
    return time.strftime("%H:%M:%S", time.localtime(common.parse_epoch(f"2033-05-18T03:{minute:02d}:{second:02d}+00:00")))


def at(second: int, minute: int = 0) -> str:
    return f"2033-05-18T03:{minute:02d}:{second:02d}+00:00"


def test_collapsed_runs_sit_at_their_first_timestamp_and_report_the_last() -> None:
    events = [
        event("task-added", {"title": "A"}, task_id=1, at=at(0)),
        *[event("controller-heartbeat", {}, at=at(10 + 4 * i), id_=10 + i) for i in range(12)],
        event("operation-running", {"route_id": "codex"}, task_id=1, at=at(30, 1)),
        *[event("controller-heartbeat", {}, at=at(40 + i, 1), id_=30 + i) for i in range(3)],
    ]
    entries = bridge_tui._activity_entries(events)
    assert [entry.text for entry in entries] == [
        "Task 1 added · A",
        f"controller alive (12 heartbeats, last {clock(54)})",
        "Started codex on task 1",
        f"controller alive (3 heartbeats, last {clock(42, 1)})",
    ]
    assert [entry.clock for entry in entries] == [clock(0), clock(10), clock(30, 1), clock(40, 1)]
    assert [entry.plumbing for entry in entries] == [False, True, False, True]


def test_interleaved_heartbeats_and_refreshes_keep_one_row_per_kind_in_first_seen_order() -> None:
    events = []
    for index in range(4):
        events.append(event("controller-heartbeat", {}, at=at(index * 10), id_=index * 2 + 1))
        events.append(event("capacity-refresh", {"provider": "codex", "capacity_brief": "5h 90%"}, at=at(index * 10 + 5), id_=index * 2 + 2))
    events.append(event("controller-heartbeat-error", {"reason": "OSError"}, at=at(50)))
    events.append(event("controller-heartbeat-error", {"reason": "OSError"}, at=at(55)))
    entries = bridge_tui._activity_entries(events)
    assert [entry.kind for entry in entries] == ["controller-heartbeat", "capacity-refresh", "controller-heartbeat-error"]
    assert [entry.clock for entry in entries] == [clock(0), clock(5), clock(50)]
    assert entries[0].text == f"controller alive (4 heartbeats, last {clock(30)})"
    assert entries[1].text == f"Capacity: codex 5h 90% (4 refreshes, last {clock(35)})"
    assert entries[2].text == f"controller heartbeat failed (OSError) (2 times, last {clock(55)})"


def test_a_single_heartbeat_is_not_decorated() -> None:
    entries = bridge_tui._activity_entries([event("controller-heartbeat", {}, at=at(1))])
    assert [(entry.clock, entry.text) for entry in entries] == [(clock(1), "controller alive")]


def activity_events() -> list[dict[str, object]]:
    return [
        event("task-added", {"title": "A"}, task_id=1, at=at(0)),
        event("operation-process", {"process_pid": 575225, "process_start": "e6af"}, at=at(1)),
        event("controller-config", {"values": {"routes": "codex"}}, at=at(2)),
        event("operation-exited", {"exit_code": 1}, task_id=1, at=at(3)),
        *[event("controller-heartbeat", {}, at=at(10 + i), id_=20 + i) for i in range(3)],
    ]


def test_activity_dims_plumbing_and_ctrl_t_hides_it() -> None:
    state = make_state({"events": activity_events()}, view="Activity")
    rows, roles = state._activity_layout()
    assert len(rows) == 5
    assert roles == ["info", "dim", "dim", "error", "dim"]
    state.handle_key("\x14")
    assert state.hide_plumbing
    rows, roles = state._activity_layout()
    assert [row.split("  ", 1)[1] for row in rows] == ["Task 1 added · A", "Provider exited (1) on task 1"]
    assert roles == ["info", "error"]
    assert "Ctrl-T show all" in bridge_tui._hint(state, 200)
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert not any("controller alive" in line for line in screen.lines)
    state.handle_key("\x14")
    assert not state.hide_plumbing and "Ctrl-T hide bookkeeping" in bridge_tui._hint(state, 200)


def test_ctrl_t_only_acts_in_activity() -> None:
    state = make_state({"events": activity_events()}, view="Tasks")
    state.handle_key("\x14")
    assert not state.hide_plumbing


def test_activity_with_only_bookkeeping_explains_how_to_see_it() -> None:
    state = make_state({"events": [event("controller-heartbeat", {}, at=at(1))]}, view="Activity")
    state.hide_plumbing = True
    rows, roles = state._activity_layout()
    assert rows == ["Only controller bookkeeping on this page (1 rows); Ctrl-T shows it."] and roles == ["dim"]
    assert make_state({}, view="Activity")._activity_layout() == (["No recent mission events."], ["dim"])


def test_activity_rows_are_drawn_with_their_role(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui, "role_attr", lambda role: {"dim": 1, "error.quiet": 2, "info": 3}.get(role, 0))
    state = make_state({"events": activity_events()}, view="Activity")
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    attrs = {text: attr for y, _x, text, attr in screen.writes if 3 <= y <= 8 and text[:1].isalpha()}
    assert attrs["Provider exited (1) on task 1"] == 2
    assert attrs["Provider process started"] == 1 and attrs["Task 1 added · A"] == 3
    clocks = [attr for y, _x, text, attr in screen.writes if 3 <= y <= 8 and ":" in text and text.count(":") == 2 and "  " not in text]
    assert clocks and set(clocks) <= {1, 0}  # clocks are muted/dim, never the row's own colour


def test_overview_recent_lists_only_operator_events() -> None:
    rows = bridge_tui._recent_event_rows(activity_events(), 100, 10)
    assert len(rows) == 2
    assert rows[0].startswith("Recent      ") and rows[0].endswith("Task 1 added · A")
    assert rows[1].startswith(" " * 12) and rows[1].endswith("Provider exited (1) on task 1")
    assert not any("controller" in row or "process" in row.lower() for row in rows)
    only_plumbing = [event("controller-heartbeat", {}, at=at(1)), event("capacity-refresh", {}, at=at(2))]
    assert bridge_tui._recent_event_rows(only_plumbing, 100, 10) == []
    assert bridge_tui._recent_event_rows(activity_events(), 100, 1) == ["Recent      " + rows[1][12:]]


def test_event_projection_keeps_capacity_percentages_as_text() -> None:
    raw = {"id": 1, "kind": "capacity-refresh", "data": {"route": "codex", "decisions": [
        {"route": "claude", "windows": [{"name": "5h", "remaining_percent": 10}]},
        {"route": "codex", "windows": [
            {"name": "5h", "remaining_percent": 95.2, "reset_epoch": NOW},
            {"name": "weekly", "remaining_percent": 21},
            {"name": "x", "kind": "opaque"},
        ]},
    ]}}
    projected = bridge_tui._project_events([raw])[0]
    assert projected["data"]["capacity_brief"] == "5h 95%, week 21%"  # type: ignore[index]
    assert bridge_tui._humanize_event(projected)[0] == "Capacity: codex 5h 95%, week 21%"
    assert bridge_tui._capacity_brief([{"route": "a", "windows": [{"name": "5h", "remaining_percent": 3}]}], "missing") == "5h 3%"
    assert bridge_tui._capacity_brief("nope") == "" and bridge_tui._capacity_brief([{"route": "a", "windows": "x"}]) == ""
    assert "capacity_brief" not in bridge_tui._project_events([{"id": 2, "kind": "provider-wait", "data": {"decisions": [{"route": "a", "windows": []}]}}])[0]["data"]  # type: ignore[index]


# --- 4. Markdown reflow in the task preview ---------------------------------------------------

def test_reflow_joins_hard_wrapped_paragraphs() -> None:
    text = "Audit how c64bridge handles the Ultimate network password (the MCP\nserver for the\nCommodore 64 Ultimate) and report\nrisks.\n\nSecond paragraph."
    lines = bridge_tui._reflow_markdown(text, 40, 20)
    assert [line for line, _ in lines] == [
        "Audit how c64bridge handles the Ultimate",
        "network password (the MCP server for the",
        "Commodore 64 Ultimate) and report risks.",
        "Second paragraph.",
    ]
    assert {role for _, role in lines} == {"info"}


def test_reflow_keeps_lists_headings_and_code() -> None:
    text = "\n".join([
        "# Goal", "Fix the", "typo.", "", "## Steps", "- first step that is long enough to need wrapping across lines",
        "  continued here", "- second", "1. numbered one", "2) numbered two", "    - nested item", "",
        "```python", "def long_function_name(argument_one, argument_two, argument_three, argument_four):", "    return 1", "```",
        "| a | b |", "after code",
    ])
    lines = bridge_tui._reflow_markdown(text, 32, 40)
    assert lines == [
        ("Goal", "heading"),
        ("Fix the typo.", "info"),
        ("Steps", "heading"),
        ("- first step that is long enough", "info"),
        ("  to need wrapping across lines", "info"),
        ("  continued here", "info"),
        ("- second", "info"),
        ("1. numbered one", "info"),
        ("2) numbered two", "info"),
        ("    - nested item", "info"),
        ("def long_function_name(argument…", "secondary"),
        ("    return 1", "secondary"),
        ("| a | b |", "secondary"),
        ("after code", "info"),
    ]
    assert not any("```" in line for line, _ in lines)


def test_reflow_limit_ends_in_an_ellipsis_and_handles_empty_input() -> None:
    lines = bridge_tui._reflow_markdown("one\n\ntwo\n\nthree\n\nfour", 20, 3)
    assert lines == [("one", "info"), ("two", "info"), ("three…", "info")]
    assert bridge_tui._reflow_markdown("", 20, 5) == []
    assert bridge_tui._reflow_markdown("   \n\n", 20, 5) == []
    assert bridge_tui._reflow_markdown("x" * 50, 10, 10)[0][0] == "x" * 10  # a long word is broken, not lost


def test_preview_uses_reflowed_prompt_and_heading_role() -> None:
    task = {"id": 5, "title": "T", "status": "READY", "priority": 1, "prompt": "# Plan\nline one\nline two", "acceptance": [], "evidence": []}
    lines = bridge_tui._order_preview_lines(task, 60, 12)
    assert ("Prompt", "Plan", "heading") in lines and ("", "line one line two", "info") in lines
    assert bridge_tui._order_preview_lines(task, 60, 0) == []


def test_preview_draws_headings_with_the_heading_role(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui, "role_attr", lambda role: {"heading": 7}.get(role, 0))
    snapshot = full_snapshot()
    snapshot["tasks"] = [{"id": 5, "title": "T", "status": "READY", "prompt": "# Plan\nbody text"}]
    state = make_state(snapshot, view="Tasks")
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert any(text == "Plan" and attr == 7 for _y, _x, text, attr in screen.writes)
    assert not any(line.lstrip().startswith("Title") for line in screen.lines)


def test_task_rows_show_only_unfinished_prerequisites() -> None:
    tasks = [
        {"id": 5, "status": "READY", "title": "done dep", "depends_on": []},
        {"id": 6, "status": "COMPLETED", "title": "finished"},
        {"id": 7, "status": "QUEUED", "title": "waits", "depends_on": [5, 6, 99]},
        {"id": 8, "status": "READY", "title": "free", "depends_on": [6]},
    ]
    rows = bridge_tui._order_list_rows(tasks)
    assert rows[2].endswith("after 5, 99  waits")
    assert "after" not in rows[0] and "after" not in rows[3]
    assert len({row.index(title) for row, title in zip(rows, ("done dep", "finished", "waits", "free"))}) == 1  # aligned
    assert bridge_tui._waiting_on({"depends_on": "x"}, tasks) == "" and bridge_tui._waiting_on({}, tasks) == ""


def test_evidence_dicts_are_shown_as_words_not_python_reprs() -> None:
    assert bridge_tui._evidence_text("{'revision': 1, 'passed': True, 'extra': None}") == "revision=1 passed=yes"
    assert bridge_tui._evidence_text("{not a dict") == "{not a dict"
    assert bridge_tui._evidence_text("{oops}") == "{oops}"
    assert bridge_tui._evidence_text("{}") == "{}"
    assert bridge_tui._evidence_text("pytest: 12 passed") == "pytest: 12 passed"
    task = {"id": 1, "title": "t", "evidence": ["{'revision': 1}"], "acceptance": []}
    assert ("Evidence", "1 item · latest: revision=1", "info") in bridge_tui._order_preview_lines(task, 80, 10)


# --- 5. Providers -------------------------------------------------------------------------------

@pytest.mark.parametrize("decision,expected,role", [
    (None, "capacity not observed", "dim"),
    ({"usable": True}, "ready", "ok"),
    ({"usable": False, "windows": [{"remaining_percent": "x"}, {"remaining_percent": 0, "reset_epoch": NOW + 7200},
                                   {"remaining_percent": 0, "reset_epoch": NOW + 3600}, {"remaining_percent": 0, "reset_epoch": NOW - 5}]},
     "rate-limited, resets in 2h", "warn"),
    ({"usable": False}, "not ready", "warn"),
    ({"usable": False, "reason": "cooldown"}, "not ready (cooldown)", "warn"),
    ({"usable": "maybe"}, "state unknown", "dim"),
])
def test_provider_state_phrases(decision, expected, role) -> None:
    assert bridge_tui._provider_state("kilo", "codex", {}, None, decision, NOW) == (expected, role)


def test_provider_state_for_the_running_owner() -> None:
    assert bridge_tui._provider_state("codex", "codex", {"id": 6}, 90, None, NOW) == ("running task 6 1m", "ok")
    assert bridge_tui._provider_state("codex", "codex", {}, None, None, NOW) == ("running", "ok")
    assert bridge_tui._provider_state("", "", {}, None, None, NOW)[0] == "capacity not observed"


def test_provider_blocks_scroll_with_the_selected_block() -> None:
    snapshot = full_snapshot()
    snapshot["stations"] = [{"id": f"p{i}", "name": f"p{i}", "model": "m"} for i in range(6)]
    state = make_state(snapshot, view="Providers")
    state.handle_key("KEY_END")
    screen = Screen(14, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert any(line.startswith("> ■ p5 ") for line in screen.lines)
    state.handle_key("KEY_PPAGE")
    assert state.selected == 0 and state.selected_station_name() == "p0"
    state.handle_key("KEY_NPAGE")
    assert state.selected == 5


def test_enter_in_providers_marks_the_provider_and_hint_says_so() -> None:
    assert "Enter mark viewed" in bridge_tui._hint(make_state(view="Providers"), 200)
    assert "Enter view station" not in bridge_tui._hint(make_state(view="Providers"), 200)


# --- 6. Actions -----------------------------------------------------------------------------------

def labels(status: str, **kwargs) -> list[str]:
    return [action.label for action in bridge_actions.actions_for(status, **kwargs)]


def test_actions_are_ordered_for_the_mission_state() -> None:
    assert labels("CREATED") == ["Start", "Add task", "Amend task", "Proposals", "Status", "Cancel mission", "Help"]
    assert labels("RUNNING") == ["Attach", "Pause", "Add task", "Steer", "Stop", "Amend task", "Proposals", "Status", "Detach", "Cancel mission", "Help"]
    assert labels("PAUSED")[:3] == ["Resume", "Attach", "Add task"]
    assert labels("STOPPED")[:3] == ["Start", "Resume", "Add task"]
    assert labels("STOPPING")[0] == "Stop"
    for terminal in ("COMPLETED", "CANCELLED", "FAILED"):
        assert labels(terminal) == ["Status", "Help"]  # read-only


def test_a_task_awaiting_approval_puts_approve_first_and_names_the_task() -> None:
    actions = bridge_actions.actions_for("RUNNING", verifying=("6", "9"))
    assert [action.label for action in actions[:5]] == ["Approve task 6", "Reject task 6", "Approve task 9", "Reject task 9", "Attach"]
    assert actions[1].typed == "/reject 6 REASON" and actions[1].handler == "prefill"
    assert actions[0].command == "approve 6" and actions[0].typed == "/approve 6" and actions[0].mutable
    assert [a.label for a in bridge_actions.actions_for("COMPLETED", verifying=("6",))] == ["Status", "Help"]


def test_pending_proposals_and_selected_task_change_the_list() -> None:
    assert labels("RUNNING", pending_proposals=2)[0] == "Proposals"
    assert "Amend task 4" in labels("RUNNING", selected_task="4")
    assert bridge_actions.Action("x", "X", "e").typed == "/x"
    assert bridge_actions.Action("steer --help", "Steer", "e").typed == "/steer"


def test_every_action_teaches_its_command_and_effect() -> None:
    for action in bridge_actions.ACTIONS:
        assert action.typed and action.effect and not action.effect.endswith(".")
        assert len(action.effect) <= 48  # label, command and effect fit one 80-column row
    typed = {action.label: action.typed for action in bridge_actions.ACTIONS}
    assert typed["Pause"] == "/pause" and typed["Add task"] == "Ctrl-E" and typed["Amend task"] == "Ctrl-R"
    assert typed["Proposals"] == "/proposals" and typed["Start"] == "/start"


def actions_state(status: str, **changes):
    snapshot = {"mission": {"status": status}, "tasks": []}
    snapshot.update(changes)
    state = make_state(snapshot, view="Actions")
    sent: list[str] = []
    state.dispatch = lambda _mission, command: sent.append(command) or "ok"
    return state, sent


def test_actions_view_follows_state_and_runs_the_selected_command() -> None:
    state, sent = actions_state("CREATED")
    assert state.rows()[0].startswith("Start") and "/start" in state.rows()[0]
    state.handle_key("\n")
    settle(state)
    assert sent == ["start"]
    state, sent = actions_state("RUNNING", tasks=[{"id": 4, "ordinal": 4, "title": "t", "status": "VERIFYING"}])
    assert state.rows()[0].startswith("Approve task 4") and "/approve 4" in state.rows()[0]
    state.handle_key("\n")
    assert state.confirm_command == "task verify --task 4 --approve"  # approval asks first
    state.handle_key("y")
    settle(state)
    assert sent == ["task verify --task 4 --approve"]


def test_pending_proposals_float_to_the_top_of_actions() -> None:
    state, _ = actions_state("RUNNING", proposal_counts={"PENDING": 1})
    assert state.palette_actions()[0].label == "Proposals"


def test_ctrl_k_opens_actions_from_any_view_with_a_ready_filter() -> None:
    state, sent = actions_state("RUNNING")
    state.view_index = VIEWS.index("Tasks")
    state.handle_key("\x0b")
    assert state.view == "Actions" and state.palette_filter_editing and state.palette_quick and state.palette_filter == ""
    assert not state.input_focused and "Esc cancel" in bridge_tui._hint(state, 200)
    for char in "pau":
        state.handle_key(char)
    assert [action.label for action in state.palette_actions()] == ["Pause"]
    state.handle_key("KEY_BACKSPACE")
    assert state.palette_filter == "pa"
    state.handle_key("KEY_ENTER")
    settle(state)
    assert sent == ["pause"] and not state.palette_filter_editing and state.palette_filter == ""
    assert not state.palette_quick


def test_ctrl_k_navigates_with_arrows_and_cancels_with_escape() -> None:
    state, sent = actions_state("RUNNING")
    state.handle_key("\x0b")
    state.handle_key("KEY_DOWN")
    state.handle_key("KEY_DOWN")
    assert state.selected == 2
    state.handle_key("KEY_UP")
    assert state.selected == 1
    for _ in range(40):
        state.handle_key("KEY_DOWN")
    assert state.selected == len(state.palette_actions()) - 1
    state.handle_key("\x1b")
    assert not state.palette_filter_editing and state.palette_filter == "" and sent == []
    state.handle_key("\x0b")
    for char in "zzz":
        state.handle_key(char)
    state.handle_key("\n")  # nothing matches: Enter just closes
    assert sent == [] and not state.palette_filter_editing


def test_ctrl_k_is_ignored_while_a_document_or_response_is_open() -> None:
    state, _ = actions_state("RUNNING")
    state.response_overlay = True
    state.handle_key("\x0b")
    assert state.view == "Actions" and not state.palette_filter_editing
    state2 = make_state(view="Tasks")
    state2.document_viewer = bridge_tui.DocumentViewer("t", "text")
    state2.handle_key("\x0b")
    assert state2.view == "Tasks"


def test_ctrl_k_works_with_the_command_line_focused_and_keeps_the_draft() -> None:
    state, _ = actions_state("RUNNING")
    state.handle_key("s")
    state.handle_key("t")
    assert state.input_focused
    state.handle_key("\x0b")
    assert state.command == "st" and not state.input_focused and state.palette_filter_editing


def test_amend_and_add_task_actions_use_the_composer_hooks() -> None:
    state, _ = actions_state("RUNNING", tasks=[{"id": 4, "title": "t", "status": "READY", "prompt": "p"}])
    add = next(action for action in state.palette_actions() if action.handler == "compose")
    state.activate_action(add)
    assert state.edit_document_requested and state.document_task_id is None
    state.edit_document_requested = False
    amend = next(action for action in state.palette_actions() if action.handler == "amend")
    state.activate_action(amend)
    assert "select a task in Tasks first" in state.output[-1]
    state.selected_task_anchor = "4"
    assert "Amend task 4" in [action.label for action in state.palette_actions()]
    state.activate_action(next(action for action in state.palette_actions() if action.handler == "amend"))
    deadline = time.monotonic() + 3
    while state.document_pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert state.document_pending or state.document_edit_pending


def test_actions_view_without_a_snapshot_treats_the_mission_as_not_started() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: {}, full=True)
    state.choose_view(VIEWS.index("Actions"))
    assert state.palette_actions()[0].label == "Start"
    assert state.action_unavailable(bridge_actions.Action("pause", "Pause", "e", mutable=True)) == ""
    state.snapshot = {"mission": {"status": "COMPLETED"}}
    assert state.action_unavailable(bridge_actions.Action("pause", "Pause", "e", mutable=True)) == "mission completed"


# --- 7. working indicator and terminal-action feedback ---------------------------------------------

def test_dispatch_shows_a_working_indicator_instead_of_a_submitted_line() -> None:
    import threading

    release = threading.Event()
    state = make_state(view="Overview")
    state.dispatch = lambda _mission, _command: release.wait(3) and "ok"
    assert state._dispatch("pause")
    base = state.working_since
    assert state.command_pending and not any("submitted" in line for line in state.output)
    first = bridge_tui._working_text(state, base)
    later = bridge_tui._working_text(state, base + 0.5)
    assert first.endswith(" working…") and later.endswith(" working…") and first != later  # animated
    text, role = bridge_tui._status_line(state, 100)
    assert text.endswith("working…") and role == "info"
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[29].strip().endswith("working…")
    compact = make_state(view="Overview", full=False)
    compact.command_pending = True
    screen = Screen(6, 100)
    bridge_tui._draw_compact(screen, compact)  # type: ignore[arg-type]
    assert screen.lines[5].strip().endswith("working…")
    release.set()
    settle(state)
    assert bridge_tui._working_text(state) == ""
    text, _ = bridge_tui._status_line(state, 100)
    assert text == "ok"  # the result replaces the indicator


def test_working_indicator_is_static_without_color(monkeypatch) -> None:
    state = make_state()
    state.command_pending = True
    for variable in ("NO_COLOR", "LLM_USAGE_NO_COLOR"):
        monkeypatch.setenv(variable, "1")
        assert bridge_tui._working_text(state, 0.0) == bridge_tui._working_text(state, 9.9) == "working…"
        monkeypatch.delenv(variable)
    monkeypatch.setenv("TERM", "dumb")
    assert bridge_tui._working_text(state, 3.0) == "working…"


def test_status_line_prefers_errors_then_working_then_messages_then_hints() -> None:
    state = make_state()
    assert bridge_tui._status_line(state, 200)[1] == "dim" and "? help" in bridge_tui._status_line(state, 200)[0]
    state.output.append("Applied: pause")
    assert bridge_tui._status_line(state, 200) == ("Applied: pause", "ok")
    state.command_pending = True
    assert bridge_tui._status_line(state, 200)[0].endswith("working…")
    state.snapshot_error = "boom"
    assert bridge_tui._status_line(state, 200) == ("boom", "error")


def test_consumed_terminal_action_leaves_no_stale_waiting_text() -> None:
    state = make_state()
    state.dispatch = lambda _mission, _command: TerminalAction("f292", None, "attach")
    state._dispatch("/attach --mission f292")
    deadline = time.monotonic() + 3
    while state.terminal_action is None and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    assert state.terminal_action == TerminalAction("f292", None, "attach")
    assert not state.command_pending and bridge_tui._working_text(state) == ""
    assert not any("waiting" in line or "submitted" in line for line in state.output)
    assert state.status_message() is None


def test_terminal_outcome_lines() -> None:
    assert bridge_tui._terminal_outcome("a", TerminalAction("f2920000", None, "attach")) == "Switched to mission f292."
    assert bridge_tui._terminal_outcome("a", TerminalAction("a", None, "attach")) == "Returned from the provider station."
    assert bridge_tui._terminal_outcome("a", TerminalAction("a", None, "detach")) == "Detached; work keeps running."


# --- 8. sessions ------------------------------------------------------------------------------------

def test_sessions_show_the_first_eight_characters_of_native_ids() -> None:
    state = make_state({"operations": [
        {"id": "0123456789abcdef", "task_id": 1, "station": "codex", "status": "EXITED",
         "native_session_id": "019a3c5e-1111-2222-3333-444455556666", "restoration_state": "RESTORED_NATIVE"},
        {"id": "fedcba9876543210", "task_id": 2, "station": "codex", "status": "EXITED", "native_session_id": None},
    ]}, view="Sessions")
    rows = state.rows()
    assert "019a3c5e" in rows[0] and "019a3c5e-" not in rows[0] and "…" not in rows[0]
    assert rows[0].split()[5] == "019a3c5e" and rows[1].split()[5] == "-"


# --- 10. failed commands do not refill the command line -----------------------------------------------

def failing_state(response: str):
    state = make_state()
    state.dispatch = lambda _mission, _command: response
    for char in "/start":
        state.handle_key(char)
    state.handle_key("\n")
    settle(state)
    return state


def test_failed_command_clears_the_line_and_is_recalled_with_up() -> None:
    state = failing_state("error: controller exited with status 2")
    assert state.command == "" and not state.input_focused
    assert state.output[-1] == "error: controller exited with status 2 · ↑ recalls the command"
    assert list(state.history) == ["/start"]
    state.handle_key("\x10")
    assert state.command == "/start" and state.input_focused
    state.handle_key("\x15")
    state.handle_key("\x1b")
    for char in "/start":
        state.handle_key(char)
    assert state.command == "/start"


def test_up_arrow_key_name_also_recalls_via_ctrl_p_binding() -> None:
    state = failing_state("error: nope")
    state.handle_key("\x10")
    assert state.command == "/start"
    state.handle_key("\x0e")
    assert state.command == ""


def test_rejected_and_unknown_outcomes_get_the_recall_hint_but_success_does_not() -> None:
    rejected = failing_state('{"status": "REJECTED", "result": {"error": "task is not ready"}}')
    assert rejected.output[-1] == "Rejected: task is not ready · ↑ recalls the command" and rejected.command == ""
    unknown = make_state()
    unknown.dispatch = lambda *_: (_ for _ in ()).throw(RuntimeError("write failed"))
    unknown._dispatch("pause")
    settle(unknown)
    assert "Outcome unknown: write failed · ↑ recalls the command" in unknown.output
    assert unknown.command == "" and "Command not confirmed" in unknown.output[-1]
    ok = failing_state('{"status": "APPLIED", "result": {}}')
    assert "recalls" not in ok.output[-1] and ok.command == ""


def test_multiline_failures_append_the_hint_as_a_last_line() -> None:
    state = failing_state("error: first line\nsecond line")
    assert list(state.output) == ["error: first line", "second line", "↑ recalls the command"]
    assert state.response_overlay and state.command == ""


def test_text_typed_while_a_command_runs_survives_a_failure() -> None:
    import threading

    release = threading.Event()
    state = make_state()
    state.dispatch = lambda _mission, _command: release.wait(3) and "error: late failure"
    state._dispatch("start")
    for char in "next":
        state.handle_key(char)
    release.set()
    settle(state)
    assert state.command == "next"


# --- real terminal ------------------------------------------------------------------------------------

@pytest.mark.skipif(__import__("shutil").which("tmux") is None, reason="tmux is required for rendered terminal assertions")
def test_real_control_view_renders_new_tabs_status_strip_and_actions(tmp_path, env) -> None:
    import os
    import sys
    from pathlib import Path

    from llm_tools.bridge_store import BridgeStore
    from llm_tools.terminal import TmuxTerminalBackend

    state_dir = tmp_path / "state"
    with BridgeStore(state_dir / "bridge.sqlite3") as store:
        mission = store.create_mission("Saved mission", "Audit the thing", cwd=tmp_path, initial_prompt="# Audit\nfirst line\nsecond line")
        mission_id = mission["id"]
        second = store.add_task(mission_id, "Follow-up work")
        store.add_dependency(mission_id, second, mission["initial_task_id"])
    root = str(Path(__file__).resolve().parents[1])
    python_path = os.pathsep.join(filter(None, (root, env.get("PYTHONPATH", ""))))
    command = ["env", "-u", "NO_COLOR", "-u", "LLM_USAGE_NO_COLOR", f"LLM_BRIDGE_STATE_DIR={state_dir}", "TERM=xterm-256color",
               f"PYTHONPATH={python_path}"]
    for name in ("HOME", "PATH", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "COVERAGE_PROCESS_START", "COVERAGE_FILE",
                 "LLM_TOOLS_NO_INHIBIT", "LLM_USAGE_NO_SERVICE", "LLM_USAGE_DISABLE_CODEX_APP_SERVER",
                 "LLM_USAGE_DISABLE_COPILOT_ADDON", "LLM_USAGE_DISABLE_COPILOT_MONTHLY", "LLM_USAGE_LIVE_FETCH_RETRIES"):
        if name in env:
            command.append(f"{name}={env[name]}")
    command.extend([sys.executable, "-m", "llm_tools.bridge", "control", "--mission", mission_id])
    backend = TmuxTerminalBackend()
    handle = backend.start(provider_argv=command, bridge_argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                           cwd=tmp_path, socket_path=tmp_path / "overhaul.sock", session_name="overhaul", size=(80, 24))

    def screen() -> list[str]:
        return backend.capture(handle, pane="provider").splitlines()

    def wait_for(predicate, message: str | None = None) -> None:
        if not _shared_wait_for(predicate, 8):
            raise AssertionError(message or "\n".join(screen()))

    try:
        wait_for(lambda: len(screen()) > 3 and "Saved mission" in screen()[0] and "[Overview]" in screen()[2])
        lines = screen()
        for name in ("Overview", "Tasks", "Providers", "Activity", "Sessions", "Health", "Actions", "Usage"):
            assert name in lines[2], lines[2]
        for old in ("Orders", "Stations", "Systems", "Palette", "Bridge]"):
            assert old not in lines[2]
        assert lines[1].startswith("not started · next: task 1 · type /start")  # status strip, not the objective
        assert "Audit the thing" not in lines[1]
        backend.send_keys(handle, pane="provider", keys="\x1bOQ")  # F2
        wait_for(lambda: "[Tasks]" in screen()[2] and any("Follow-up work" in line for line in screen()))
        assert any(line.strip().startswith("Revision") for line in screen())
        assert any("first line second line" in line for line in screen())  # reflowed prompt
        assert any("after 1" in line and "Follow-up work" in line for line in screen())  # dependency shown in the list
        backend.send_keys(handle, pane="provider", keys="\x0b")  # Ctrl-K opens Actions with a filter
        wait_for(lambda: "[Actions]" in screen()[2])
        wait_for(lambda: any(line.lstrip("> ").startswith("Start") and "/start" in line for line in screen()))
        assert not any("…" in line for line in screen()[3:11])  # action rows fit 80 columns
        before_escape = screen()
        backend.send_keys(handle, pane="provider", keys="\x1b")
        # curses waits ESCDELAY (1s) to tell a standalone Escape from an escape sequence; the redraw shows it was taken.
        wait_for(lambda: screen() != before_escape, "Escape was not processed")
        backend.send_keys(handle, pane="provider", keys="\x1bOS")  # F4
        wait_for(lambda: "[Activity]" in screen()[2])
        assert any("Task 1 added" in line for line in screen())
        assert not any("task-added" in line for line in screen())
    finally:
        backend.stop(handle)


def test_reject_action_prefills_the_command_line_for_the_reason() -> None:
    state, sent = actions_state("RUNNING", tasks=[{"id": 4, "title": "t", "status": "VERIFYING"}])
    assert state.rows()[1].startswith("Reject task 4") and "/reject 4 REASON" in state.rows()[1]
    state.handle_key("KEY_DOWN")
    state.handle_key("\n")
    assert state.command == "reject 4 " and state.command_cursor == 9 and state.input_focused and sent == []
