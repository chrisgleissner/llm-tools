"""Operator-facing wording and layout of the Bridge control console."""

from __future__ import annotations

import time

import pytest

from llm_tools import bridge_draw, bridge_tui
from llm_tools.bridge_input import cell_width


class Screen:
    """Records every cell write so tests can assert final line contents."""

    def __init__(self, height: int, width: int) -> None:
        self.height, self.width = height, width
        self.lines = [""] * height
        self.writes: list[tuple[int, int, str, int]] = []

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.lines = [""] * self.height
        self.writes = []

    def addnstr(self, y: int, x: int, text: str, count: int, attr: int = 0) -> None:
        self.writes.append((y, x, text[:count], attr))
        line = self.lines[y].ljust(x)
        self.lines[y] = line[:x] + text[:count] + line[x + len(text[:count]):]

    def noutrefresh(self) -> None:
        pass

    def move(self, *_args: int) -> None:
        pass


@pytest.fixture(autouse=True)
def _quiet_curses(monkeypatch):
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "curs_set", lambda _value: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "2000000000")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)


def make_state(snapshot: dict[str, object] | None = None, *, full: bool = True, view: str = "Overview"):
    state = bridge_tui._ConsoleState("M1", lambda *_: "ok", lambda *_: {}, full=full)
    state.snapshot = bridge_tui._bounded_snapshot(snapshot or {})
    state.choose_view(bridge_tui.VIEWS.index(view))
    return state


def full_snapshot() -> dict[str, object]:
    now = 2_000_000_000
    return {
        "mission": {"id": "M1", "name": "Fix the typo", "objective": "Fix the typo in README", "status": "RUNNING"},
        "tasks": [
            {"id": 1, "title": "done soon", "status": "VERIFYING"},
            {"id": 2, "title": "second task", "status": "RUNNING"},
            {"id": 3, "title": "third", "status": "QUEUED"},
            {"id": 4, "title": "stuck", "status": "BLOCKED"},
        ],
        "current_task": {"id": 2, "title": "second task", "status": "RUNNING"},
        "task_counts": {"QUEUED": 1, "RUNNING": 1, "VERIFYING": 1, "BLOCKED": 1, "COMPLETED": 0},
        "proposal_counts": {"PENDING": 2},
        "pending_command_count": 0,
        "pending_commands": [],
        "criteria": {"verified": 0, "total": 4, "complete": False},
        "lifecycle": {"status": "RUNNING", "controller": "healthy", "controller_event_at": "2033-05-18T03:20:00+00:00",
                      "heartbeat": {"reported_state": "ACTIVE", "age_seconds": 2, "owner_lock_held": True,
                                    "desired_mission_status": "RUNNING", "current_liveness": "unknown"}},
        "station_context": {"executing_operation": "op-1", "operation_status": "RUNNING", "last_launched_station": "codex"},
        "capacity": {"source": "persisted runtime event", "source_age_seconds": 12, "selection": {
            "route": "codex", "selected_model": "gpt-5", "rotation_reason": "preferred",
            "decisions": [{"route": "codex", "selected_model": "gpt-5", "windows": [
                {"name": "5h", "kind": "reset_window", "remaining_percent": 100.0, "reset_epoch": now + 4 * 3600 + 59 * 60},
            ]}]}},
        "events": [],
    }


# --- _put keeps layout spacing -------------------------------------------------

def test_put_sanitizes_controls_without_collapsing_spaces() -> None:
    screen = Screen(1, 40)
    bridge_tui._put(screen, 0, 0, "A  B\tC\x1b[31m   D", 40)  # type: ignore[arg-type]
    assert screen.lines[0] == "A  B    C [31m   D"
    assert "\x1b" not in screen.lines[0]


# --- header ----------------------------------------------------------------------

def test_header_uses_dot_separators_and_semantic_status_color() -> None:
    state = make_state(full_snapshot())
    assert bridge_tui._header(state, 100) == "LLM BRIDGE · ▌ Fix the typo · ● RUNNING"
    assert bridge_tui._mission_status_role("RUNNING") == "state_running"
    assert bridge_tui._mission_status_role("PAUSED") == "state_paused"
    assert bridge_tui._mission_status_role("FAILED") == "state_failed"
    assert bridge_tui._mission_status_role("CREATED") == "state_waiting"
    assert bridge_tui._mission_status_role("WHATEVER") == "info"
    state.snapshot["mission"]["status"] = "PAUSED"  # type: ignore[index]
    assert bridge_tui._header(state, 100).endswith("· ‖ PAUSING")  # provider still running
    screen = Screen(34, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[0].startswith("LLM BRIDGE · ▌ Fix the typo · ‖ PAUSING")
    assert [w[2] for w in screen.writes[:6]] == ["LLM BRIDGE", " · ", "▌", " Fix the typo", " · ", "‖ PAUSING"]


def test_header_states_before_first_snapshot_and_for_long_names() -> None:
    state = bridge_tui._ConsoleState("M1", lambda *_: "", lambda *_: {}, full=True)
    assert bridge_tui._header(state, 80).endswith("· LOADING")
    state.snapshot_error = "boom"
    assert bridge_tui._header(state, 80).endswith("· ✕ UNAVAILABLE")
    state.snapshot = {"mission": {"name": "x" * 200, "status": "RUNNING"}}
    header = bridge_tui._header(state, 40)
    assert cell_width(header) <= 40 and "…" in header and header.endswith("· ● RUNNING")


# --- footer hints ------------------------------------------------------------------

@pytest.mark.parametrize("view,expected", [
    ("Overview", ["o objective", "Ctrl-E add task", "Ctrl-K actions", "Ctrl-D exit"]),
    ("Tasks", ["Enter inspect", "Ctrl-E add task", "Ctrl-R amend", "v view prompt", "Ctrl-F find"]),
    ("Providers", ["Enter mark viewed", "↑↓ select"]),
    ("Health", ["↑↓ scroll"]),
    ("Activity", ["PgUp older", "PgDn newer", "Ctrl-T hide bookkeeping"]),
    ("Sessions", ["↑↓ scroll"]),
    ("Actions", ["Enter run action", "Ctrl-K filter"]),
    ("Usage", ["Shift-←/→ pan"]),
])
def test_each_view_lists_its_own_keys_and_ends_with_help(view: str, expected: list[str]) -> None:
    state = make_state(view=view)
    hint = bridge_tui._hint(state, 220)
    for item in expected:
        assert item in hint
    # The empty command line carries "type a command"; the footer does not repeat it.
    assert hint.endswith(" · ? help") and "type a command" not in hint
    # Keys that only work in another view must not be advertised.
    if view != "Tasks":
        assert "v view prompt" not in hint and "Ctrl-F" not in hint
    if view != "Overview":
        assert "o objective" not in hint


def test_hint_drops_whole_items_to_fit_and_keeps_help() -> None:
    state = make_state(view="Tasks")
    previous = None
    items = set(bridge_tui._hint_items(state)[0])
    for width in (120, 100, 80, 60, 40):
        hint = bridge_tui._hint(state, width)
        assert cell_width(hint) <= width and hint.endswith("? help")
        assert set(hint.split(" · ")[:-1]) <= items  # whole items only: never cut mid-word
        count = len(hint.split(" · "))
        assert previous is None or count <= previous
        previous = count
    assert bridge_tui._hint(state, 6) == "? help"
    assert bridge_tui._hint(state, 3) == "? …"


def test_hint_follows_input_focus_and_modes(monkeypatch) -> None:
    state = make_state(view="Tasks")
    state.input_focused = True
    focused = bridge_tui._hint(state, 140)
    assert "Tab complete" in focused and "Esc back to view" in focused and "? help" not in focused
    state.input_focused = False
    state.search_editing = True
    assert "Enter search" in bridge_tui._hint(state, 140)
    state.search_editing = False
    state.palette_filter_editing = True
    assert "type to filter actions" in bridge_tui._hint(state, 140)
    state.palette_filter_editing = False
    state.task_detail = True
    assert bridge_tui._hint(state, 140).startswith("Esc back")
    bridge = make_state(view="Overview", full=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    assert "Ctrl-] P provider" not in bridge_tui._hint(bridge, 140)  # any tmux is not a station
    monkeypatch.setenv("TMUX", "/run/user/1000/llm-bridge/m-0123456789abcdef0123.sock,123,0")
    assert "Ctrl-] P provider" in bridge_tui._hint(bridge, 140) and "Ctrl-] z expand" in bridge_tui._hint(bridge, 140)
    monkeypatch.setenv("TMUX", "/tmp/lb-1000/m-0123456789abcdef0123.sock,123,0")
    assert bridge_tui._hint(bridge, 140).startswith("Ctrl-] P provider")


# --- Bridge overview -----------------------------------------------------------------

def test_bridge_overview_rows_are_labelled_and_plain() -> None:
    rows = make_state(full_snapshot()).rows()
    assert rows[0] == "Controller  active · heartbeat 2s ago"
    assert rows[1] == "Now         task 2  ● RUNNING  second task"
    assert rows[2] == "Next        task 3  ○ QUEUED  third"
    assert rows[3] == "Tasks       1 running · 1 verifying · 1 queued · 1 blocked · 2 proposals"
    needs = [row for row in rows if row.startswith("Needs you") or row.startswith(" " * 12 + "task") or "proposals to" in row]
    assert needs[0] == "Needs you   task 1 awaits your approval — type /approve 1 (or F7 Actions)"
    assert any("2 agent task proposals to review" in row for row in rows)
    assert any("task 4 BLOCKED — inspect in Tasks" in row for row in rows)
    assert "Criteria    0/4 verified" in rows
    assert rows[-1].startswith("Capacity    ■ codex / gpt-5 (active) · 5h 100% left, resets 4h 59m")
    text = "\n".join(rows)
    assert "ops " not in text and "events " not in text and "Pending commands" not in text
    assert all(row[11] == " " and (len(row) == 12 or row[12] != " " or row[:12].strip() == "") for row in rows)  # label column


def test_bridge_overview_quiet_mission_hides_empty_rows() -> None:
    rows = make_state({"mission": {"status": "CREATED"}}).rows()
    assert rows == [
        "Controller  not started — type start",
        "Now         idle",
        "Next        nothing ready",
        "Tasks       none yet — Ctrl-E adds a task",
    ]
    queued = make_state({"mission": {"status": "RUNNING"}, "pending_commands": [{}, {}, {}]}).rows()
    assert "Queue       3 commands waiting for the controller" in queued


@pytest.mark.parametrize("snapshot,expected", [
    ({}, "status not loaded yet"),
    ({"mission": {"status": "COMPLETED"}}, "mission completed — controller finished"),
    ({"mission": {"status": "STOPPED"}}, "stopped — type start or resume"),
    ({"mission": {"status": "STOPPING"}}, "stopping after the current step"),
    ({"mission": {"status": "RUNNING"}}, "no heartbeat recorded yet"),
    ({"mission": {"status": "RUNNING"}, "lifecycle": {"heartbeat": {"reported_state": "ACTIVE", "age_seconds": 400}}},
     "silent for 6m — controller may have stopped (type status or resume)"),
    ({"mission": {"status": "PAUSED"}, "lifecycle": {"heartbeat": {"reported_state": "ACTIVE", "age_seconds": 5}}},
     "paused, controller active · heartbeat 5s ago"),
    ({"mission": {"status": "PAUSED"}, "station_context": {"executing_operation": "op", "operation_status": "RUNNING"}},
     "pausing after the current provider invocation"),
])
def test_controller_line_is_derived_from_lifecycle(snapshot, expected) -> None:
    assert bridge_tui._controller_line(bridge_tui._bounded_snapshot(snapshot)) == expected


def test_overview_rows_are_not_selectable_and_scroll_without_markers(monkeypatch) -> None:
    monkeypatch.setattr(bridge_draw, "role_attr", lambda _role: 0)
    state = make_state(full_snapshot())
    assert not state.selectable
    screen = Screen(34, 120)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert not any(line.startswith(">") for line in screen.lines)
    assert not any(attr for _y, _x, text, attr in screen.writes if text.startswith("Controller"))
    state.handle_key("KEY_DOWN")
    state.handle_key("KEY_DOWN")
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert state.selected == 0  # everything fits, so scrolling is clamped
    assert not any(line.startswith(">") for line in screen.lines)
    tiny = Screen(10, 120)
    state.selected = 50
    bridge_tui._draw_full(tiny, state)  # type: ignore[arg-type]
    assert 0 < state.selected < 50  # clamped to the last page
    assert [view for view in bridge_tui.VIEWS if make_state(view=view).selectable] == ["Tasks", "Providers", "Actions"]
    mouse_state = make_state(full_snapshot())
    mouse_state.handle_mouse(5, 5, 34, 120)
    assert mouse_state.selected == 0


# --- capacity -----------------------------------------------------------------------------

def test_capacity_line_matches_documented_wording() -> None:
    now = 2_000_000_000
    line = bridge_tui._capacity_summaries({
        "station_context": {"executing_operation": "op", "operation_status": "RUNNING", "last_launched_station": "codex"},
        "capacity": {"selection": {"route": "codex", "decisions": [{"route": "codex", "windows": [
            {"name": "5h", "kind": "reset_window", "remaining_percent": "100.0", "estimated_remaining": "unknown", "reset_epoch": now + 4 * 3600 + 59 * 60},
            {"name": "weekly", "kind": "reset_window", "remaining_percent": "21.0", "estimated_remaining": "1h 52m", "reset_epoch": now + 5 * 86400 + 19 * 3600 + 28 * 60},
        ]}]}},
    })[0]
    assert line == "codex (active) · 5h 100% left, resets 4h 59m · weekly 21% left, empty in ~1h 52m at this pace, resets 5d 19h"
    expired = bridge_tui._window_summary({"name": "5h", "remaining_percent": 1, "reset_epoch": now - 5}, now)
    assert expired == "5h 1% left, resets now"


# --- events -------------------------------------------------------------------------------

def event(kind: str, data: dict[str, object] | None = None, *, task_id: object = None, operation_id: object = None, at: str = "2033-05-18T03:33:20+00:00", id_: int = 1):
    row: dict[str, object] = {"id": id_, "kind": kind, "created_at": at, "data": data or {}}
    if task_id is not None:
        row["task_id"] = task_id
    if operation_id is not None:
        row["operation_id"] = operation_id
    return row


@pytest.mark.parametrize("row,expected", [
    (event("mission-created", {"name": "Demo", "cwd": "/x"}), "mission created · Demo"),
    (event("task-added", {"title": "Do it", "priority": 0}, task_id=3), "task 3 added · Do it"),
    (event("task-ready", {"from": "QUEUED", "to": "READY"}, task_id=3), "task 3 QUEUED → READY"),
    (event("task-status", {"from": "READY", "to": "QUEUED", "reason": "dependency-added"}, task_id=3), "task 3 READY → QUEUED (dependency-added)"),
    (event("mission-status", {"from": "CREATED", "to": "RUNNING"}), "mission CREATED → RUNNING"),
    (event("operation-prepared", {"iteration": 1, "station": "codex"}, task_id=2, operation_id="abcdef0123456789"), "op abcdef01 prepared · task 2 · codex"),
    (event("operation-running", {"route_id": "codex", "model": "gpt-5", "process_pid": 77, "pane_id": ""}, task_id=2, operation_id="abcdef0123456789"),
     "op abcdef01 running · task 2 · codex gpt-5 · pid 77"),
    (event("operation-exited", {"exit_code": 0, "task_status": "VERIFYING", "logs_path": None}, task_id=2, operation_id="abcdef0123456789"),
     "op abcdef01 exited 0 · task 2 · task now VERIFYING"),
    (event("provider-selection", {"route": "kilo", "provider": "kilo", "selected_model": "m", "rotation_reason": "preferred"}), "selected kilo · m · preferred"),
    (event("capacity-refresh", {"provider": "codex"}), "capacity refreshed codex"),
    (event("command-queued", {"command": "task.add", "command_id": "c"}), "command queued: task.add"),
    (event("command-applied", {"command": "pause"}), "command applied: pause"),
    (event("native-session-restoration", {"provider": "codex", "session_id": "019a3c5e-1111-2222", "restoration_state": "RESTORED_NATIVE"}, operation_id="abcdef0123456789"),
     "session restored_native · codex · 019a3c5e-1111 · op abcdef01"),
    (event("operation-uncertain", {"reason": "controller exited"}, operation_id="abcdef0123456789"), "op abcdef01 outcome unknown · controller exited"),
    (event("repository-checkpoint", {"task_id": 2, "revision": 4, "checkpoint": {"fingerprint": "abc"}}), "repository checkpoint saved · task 2 · revision 4"),
    (event("task-verified", {"evidence_count": 2, "approved": True}, task_id=2), "task 2 verified · 2 evidence approved by operator"),
    (event("verification-failed", {"passed": False, "exit_code": 1, "stderr": None}, task_id=2), "task 2 verification failed · passed=no exit_code=1"),
    (event("provider-handoff", {"exit_code": 1, "failure_count": 1, "max_failures": 2}, task_id=2), "task 2 handed off after exit 1 · failure 1/2"),
    (event("provider-handoff-selected", {"station": "claude"}), "handoff to claude"),
    (event("provider-handoff-exhausted", {"failures": 3, "max_failures": 2}, task_id=2), "task 2 out of handoffs · 3 failures, limit 2"),
    (event("provider-wait", {"target_epoch": 2_000_000_000 + 3600, "reason": "rate-limited"}), "waiting for capacity · resumes in 1h · rate-limited"),
    (event("controller-heartbeat", {"controller_state": "ACTIVE"}), "ACTIVE"),
    (event("controller-mode", {"verified": True, "actual_mode": "headless", "extra": None, "nested": {"a": 1, "b": []}}), "verified=yes actual_mode=headless nested.a=1"),
    (event("something-new", {}), ""),
    ({"kind": "plain", "summary": "free text"}, "free text"),
])
def test_event_summaries_are_human_sentences(row, expected, monkeypatch) -> None:
    assert bridge_tui._event_summary(row) == expected


def test_event_data_is_a_bounded_dict_not_a_repr() -> None:
    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}, "list": list(range(50)), "text": "x" * 1000,
            "many": {str(i): i for i in range(100)}, "obj": object()}
    projected = bridge_tui._project_events([{"id": 1, "kind": "k", "created_at": "t", "task_id": 9, "operation_id": "o", "data": deep}])[0]
    data = projected["data"]
    assert isinstance(data, dict) and projected["task_id"] == "9"
    assert data["a"] == {"b": {"c": "1 fields"}}  # bounded depth
    assert len(data["list"]) == 6 and len(data["text"]) == 160
    assert len(data["many"]) <= bridge_tui._MAX_DETAIL_ITEMS and str(data["obj"]).startswith("<object")
    assert bridge_tui._bounded_event_data([{"a": [1]}], depth=0) == "1 items"
    huge = {"scalar": 1, **{f"k{i}": {f"n{j}": "z" * 150 for j in range(12)} for i in range(12)}}
    shrunk = bridge_tui._project_events([{"id": 2, "kind": "k", "payload": huge}])[0]["data"]
    assert shrunk == {"scalar": 1}  # oversized nested data collapses to its scalar fields


def test_log_rows_are_timestamped_humanized_and_collapse_heartbeats() -> None:
    rows = [
        event("task-added", {"title": "A"}, task_id=1, at="2033-05-18T03:33:20+00:00", id_=1),
        *[event("controller-heartbeat", {"controller_state": "ACTIVE"}, at=f"2033-05-18T03:33:{21 + i}+00:00", id_=2 + i) for i in range(5)],
        event("capacity-refresh", {"provider": "codex"}, at="2033-05-18T03:34:00+00:00", id_=7),
        event("capacity-refresh", {"provider": "codex"}, at="2033-05-18T03:34:30+00:00", id_=8),
        event("operation-uncertain", {"reason": "x"}, at="2033-05-18T03:35:00+00:00", id_=9),
    ]
    state = make_state({"events": rows}, view="Activity")
    lines = state.rows()
    from llm_tools import common
    first, last = (time.strftime("%H:%M:%S", time.localtime(common.parse_epoch(f"2033-05-18T03:33:{n}+00:00"))) for n in (21, 25))
    assert len(lines) == 4  # 7 heartbeat/refresh events collapse into 2 rows
    assert lines[0].endswith("  Task 1 added · A")
    assert lines[1].startswith(first) and lines[1].endswith(f"controller alive (5 heartbeats, last {last})")
    assert "Capacity refreshed: codex (2 refreshes, last " in lines[2]
    assert lines[3].endswith("Provider outcome unknown · x")
    assert "{" not in "".join(lines) and "'" not in "".join(lines) and "controller-heartbeat" not in "".join(lines)
    screen = Screen(34, 120)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert any(line.lstrip().startswith(first) for line in screen.lines)


def test_log_page_reads_from_a_real_projection() -> None:
    state = make_state({"events": [event("task-ready", {"from": "QUEUED", "to": "READY"}, task_id=1)]}, view="Activity")
    assert state.rows()[0].endswith("  Task 1 ready")


# --- systems --------------------------------------------------------------------------------

def test_systems_view_is_sectioned_relative_and_never_a_repr() -> None:
    snapshot = full_snapshot()
    snapshot.update({
        "station_mode": {"configured_mode": "native", "actual_mode": "headless", "source": "x"},
        "execution": {"iteration": 2, "operation_elapsed_seconds": 75, "seconds_since_verified_progress": None, "progress_source": "none"},
        "health": {"status": "warning", "consecutive_failed_operations": 2, "max_provider_failures": 4, "stall_warning": "2 operations without repository change"},
        "persistence": {"database": "/state/bridge.sqlite3", "schema_version": 6, "journal_mode": "wal"},
        "last_meaningful_progress": {"event_id": 4, "kind": "task-verified", "at": "2033-05-18T03:20:00+00:00"},
        "suspend": {"status": "suspend_until_renewal", "at": None, "target_epoch": 2_000_000_000 + 7200, "reason": "all rate limited"},
        "controller": "healthy",
        "routes": "codex,claude",
    })
    snapshot["capacity"]["wait"] = {"target_epoch": 2_000_000_000 + 600, "reason": "rate-limited"}  # type: ignore[index]
    snapshot["capacity"]["selection"]["all_rate_limited"] = True  # type: ignore[index]
    rows = make_state(snapshot, view="Health").rows()
    text = "\n".join(rows)
    for label in ("Mode", "Lifecycle", "Controller heartbeat", "Execution", "Health", "Persistence", "Criteria", "Suspend", "Capacity", "Last progress"):
        assert any(row.startswith(label + " ") for row in rows), label
    assert "Mode                  configured native · actual headless" in rows
    assert "Lifecycle             RUNNING · controller healthy (verified" in text
    assert "Execution             iteration 2 · operation running 1m · last verified progress" not in text
    assert "Execution             iteration 2 · operation running 1m · no verified progress yet" in rows
    assert "2 consecutive failed operations (limit 4) · 2 operations without repository change" in text
    assert "Persistence           sqlite schema 6 · wal journal · /state/bridge.sqlite3" in rows
    assert "Suspend               suspend_until_renewal · until 2h · all rate limited" in rows
    assert "waiting in 10m · rate-limited" in text and "all routes rate-limited" in text
    assert "{" not in text and "None" not in text and "epoch" not in text and "=" not in text
    assert all(row[:22].rstrip() == row[:22].strip() for row in rows)


def test_systems_wraps_long_values_under_the_section_label() -> None:
    state = make_state({"persistence": {"database": "/very/long/" + "path/" * 30 + "bridge.sqlite3"}}, view="Health")
    screen = Screen(30, 60)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    body = [line for line in screen.lines[3:12] if line.strip()]
    assert body[0].lstrip().startswith("Persistence")
    assert len(body) > 2
    assert all(line.startswith(" " * 24) for line in body[1:])  # gutter + label column
    assert all(cell_width(line) <= 60 for line in screen.lines)


# --- stations / sessions / palette / usage ---------------------------------------------------

def test_providers_view_shows_one_block_per_provider_and_hides_unknown() -> None:
    state = make_state({
        "stations": [
            {"id": "codex", "name": "codex", "model": "unknown", "observed_model": "unknown", "status": "configured"},
            {"id": "kilo-zai-glm", "name": "kilo-zai-glm", "model": "zai/glm-5.2", "observed_model": "zai/glm-5.2", "status": "configured"},
            {"id": "old", "name": "old", "model": "unknown", "status": "historical"},
        ],
        "station_config": {"configured_mode": "native"},
        "station_context": {"executing_operation": "0123456789abcdef", "operation_status": "RUNNING", "viewed_station": "kilo-zai-glm",
                            "last_launched_station": "codex"},
        "operations": [{"id": "op-9999999999", "task_id": 4, "station": "codex", "status": "EXITED", "exit_code": 0, "model": "unknown"}],
    }, view="Providers")
    rows = state.rows()
    assert rows[0] == "Viewing kilo-zai-glm · last launched codex · operation 01234567 RUNNING"
    assert rows[1] == "mode native"
    headers = [row for row in rows if not row.startswith("  ") and row not in rows[:2]]
    assert len(headers) == 3 and state.selection_rows() == [rows.index(header) for header in headers]
    assert headers[0].startswith("■ codex  default model  ● running")
    assert headers[1].startswith("■ kilo-zai-glm  model zai/glm-5.2") and headers[1].endswith("[viewed]")
    assert headers[2].endswith("[historical]")
    assert "  capacity: no observation yet" in rows
    assert "  recent: task 4 · exited · exit 0" in rows
    assert "  recent: no operations yet" in rows
    assert "unknown" not in "\n".join(rows)
    assert make_state({"stations": [{"id": "a"}]}, view="Providers").rows()[1].startswith("Enter marks")
    assert make_state({"operations": [{"station": "x", "status": "EXITED"}]}, view="Providers").selection_rows() == []


def test_providers_blocks_show_live_state_capacity_and_pace() -> None:
    snapshot = full_snapshot()
    now = 2_000_000_000
    snapshot["stations"] = [
        {"id": "codex", "name": "codex", "model": "gpt-5", "observed_model": "gpt-5.1"},
        {"id": "claude", "name": "claude", "model": "sonnet"},
        {"id": "kilo", "name": "kilo", "model": "m"},
        {"id": "zai", "name": "zai", "model": "g"},
    ]
    snapshot["capacity"] = {"selection": {"route": "codex", "decisions": [
        {"route": "codex", "usable": True, "windows": [
            {"name": "5h", "kind": "reset_window", "remaining_percent": 95.0, "reset_epoch": now + 4 * 3600, "estimated_remaining": "~3h"},
            {"name": "weekly", "kind": "reset_window", "remaining_percent": 21.0, "reset_epoch": now + 5 * 86400},
        ]},
        {"route": "claude", "usable": True, "windows": [{"name": "5h", "kind": "reset_window", "remaining_percent": 60.0}]},
        {"route": "kilo", "usable": False, "windows": [
            {"name": "5h", "kind": "reset_window", "remaining_percent": 0.0, "reset_epoch": now + 3600},
            {"name": "weekly", "kind": "reset_window", "remaining_percent": 50.0}]},
        {"route": "zai", "usable": False, "reason": "blocked", "windows": []},
    ]}}
    snapshot["execution"] = {"operation_elapsed_seconds": 240}
    snapshot["stations"][0]["status"] = "configured"  # type: ignore[index]
    rows = make_state(snapshot, view="Providers").rows()
    codex = next(row for row in rows if row.startswith("■ codex"))
    assert codex == "■ codex  model gpt-5 (live gpt-5.1)  ● running task 2 4m"
    assert any(row.startswith("  5h 95% left") and "resets 4h" in row and "at this pace" in row for row in rows)
    assert any(row.startswith("  weekly 21% left") and "resets 5d" in row for row in rows)
    assert next(row for row in rows if row.startswith("■ claude")) == "■ claude  model sonnet  ○ ready"
    assert next(row for row in rows if row.startswith("■ kilo")) == "■ kilo  model m  rate-limited, resets in 1h"
    assert next(row for row in rows if row.startswith("■ zai")) == "■ zai  model g  not ready (blocked)"
    assert "  capacity unavailable" in rows
    roles = make_state(snapshot, view="Providers").row_roles()
    assert roles is not None and roles[rows.index(codex)] == "ok"


def test_providers_selection_moves_between_blocks_and_enter_marks_viewed() -> None:
    dispatched = []
    state = make_state({"stations": [{"id": "a", "name": "a", "model": "x"}, {"id": "b", "name": "b", "model": "y"}]}, view="Providers")
    state.dispatch = lambda mission, command: dispatched.append(command) or "ok"
    rows = state.rows()
    assert state.selected_row() == 2
    state.handle_key("KEY_DOWN")
    assert state.selected == 1 and state.selected_row() == rows.index(next(r for r in rows if r.startswith("■ b ")))
    state.handle_key("KEY_DOWN")
    assert state.selected == 1  # clamped to the last block, not the last row
    state.handle_key("KEY_HOME")
    assert state.selected == 0
    state.handle_key("KEY_END")
    assert state.selected == 1
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert any(line.startswith("> ■ b ") for line in screen.lines)
    state.handle_mouse(3 + 2, 5, 30, 100)  # click inside block a
    assert state.selected == 0
    state.handle_mouse(3, 5, 30, 100)  # header rows are not blocks
    assert state.selected == 0
    state.handle_key("KEY_END")
    state.handle_key("\n")
    _wait_for_dispatch(state)
    assert dispatched == ["station b"]


def _wait_for_dispatch(state) -> None:
    deadline = time.monotonic() + 3
    while state.command_pending and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    assert not state.command_pending


def test_sessions_view_is_an_aligned_table_with_short_ids() -> None:
    state = make_state({"operations": [
        {"id": "0123456789abcdef", "task_id": 1, "station": "codex", "route_id": None, "model": "gpt", "status": "EXITED",
         "native_session_id": "abc", "restoration_state": "RESTORED_NATIVE"},
        {"id": "fedcba9876543210", "task_id": 22, "station": "claude", "route_id": "claude-sonnet", "model": None, "status": "RUNNING",
         "native_session_id": None, "restoration_state": "unknown"},
    ]}, view="Sessions")
    header = state.column_header()
    rows = state.rows()
    assert header == "op        task  provider       model  status     native session  restoration"
    assert rows[0] == "01234567  1     codex          gpt    EXITED     abc             RESTORED_NATIVE"
    assert rows[1] == "fedcba98  22    claude-sonnet  -      ● RUNNING  -               -"
    screen = Screen(30, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[3].strip() == header.strip() and screen.lines[4].strip() == rows[0]


def test_actions_rows_teach_the_slash_command_and_align() -> None:
    state = make_state(full_snapshot(), view="Actions")
    rows = state.rows()
    actions = state.palette_actions()
    label_width = max(len(action.label) for action in actions)
    typed_width = max(len(action.typed) for action in actions)
    assert len(rows) == len(actions)
    for row, action in zip(rows, actions):
        assert row.startswith(f"{action.label:<{label_width}}  {action.typed:<{typed_width}}  {action.effect}")
    pause = next(row for row in rows if row.startswith("Pause"))
    assert "/pause" in pause and "Hold after the current provider turn" in pause
    amend = next(row for row in rows if row.startswith("Amend task"))
    assert amend.endswith("· unavailable: select a task in Tasks first") and "Ctrl-R" in amend
    state.selected_task_anchor = "7"
    rows = state.rows()
    assert next(row for row in rows if row.startswith("Amend task 7")).count("unavailable") == 0
    # View tabs are not repeated as actions.
    text = "\n".join(rows)
    for duplicate in ("Inspect saved", "Return to the overview", "Read recent operational history", "provider and route capacity"):
        assert duplicate not in text


def test_usage_header_has_no_none_age_and_rows_are_clipped_not_wrapped() -> None:
    state = make_state({"usage": {"status": "refreshing", "age_seconds": None, "text": "Provider  " + "X" * 200}}, view="Usage")
    assert state.rows()[0] == "Usage · refreshing · first sample pending"
    assert "None" not in state.rows()[0]
    state.snapshot["usage"] = {"status": "unavailable", "age_seconds": 125, "error": "RuntimeError", "text": "row"}  # type: ignore[index]
    assert state.rows()[0] == "Usage · unavailable · updated 2m ago · last refresh failed: RuntimeError"
    state.snapshot["usage"] = {"status": "ready", "age_seconds": 3, "text": "Provider  " + "X" * 200 + "END"}  # type: ignore[index]
    screen = Screen(20, 80)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    rendered = [line for line in screen.lines if "XXXX" in line]
    assert len(rendered) == 1 and "END" not in rendered[0] and cell_width(rendered[0]) <= 80
    state.handle_key("KEY_SRIGHT")
    state.handle_key("KEY_SRIGHT")
    for _ in range(40):
        state.handle_key("KEY_SRIGHT")
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert state.usage_hscroll == 10 + 200 + 3 - 78  # clamped to the widest row
    assert any(line.rstrip().endswith("END") for line in screen.lines)
    state.handle_key("KEY_SLEFT")
    assert state.usage_hscroll == (10 + 200 + 3 - 78) - 8
    other = make_state(view="Health")
    other.handle_key("KEY_SRIGHT")
    assert other.usage_hscroll == 0


# --- command line ----------------------------------------------------------------------------

def test_command_line_shows_prompt_glyph_and_dim_placeholder_only_when_idle() -> None:
    state = make_state(full_snapshot())
    screen = Screen(34, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[32] == "› type a command · /help · Ctrl-K actions"
    placeholder = next(write for write in screen.writes if write[2] == "type a command · /help · Ctrl-K actions" and write[0] == 32)
    assert placeholder[3] & bridge_tui.curses.A_DIM  # dim even without color support
    state.handle_key("p")
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[32] == "› p"
    state.handle_key("\x15")
    state.handle_key("\x1b")
    state.view_index = bridge_tui.VIEWS.index("Actions")
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[32] == "› type a command · Ctrl-K filters actions"
    state.command_pending = True
    assert bridge_tui._placeholder(state) == "running a command — wait for its result"


def test_slash_commands_complete_dispatch_and_confirm_like_bare_commands() -> None:
    assert "/pause" in bridge_tui._command_completions("/pa")
    assert all(item.startswith("/pa") for item in bridge_tui._command_completions("/pa"))
    assert "/task" in bridge_tui._command_completions("/") and "pause" in bridge_tui._command_completions("")
    assert "/task add" in bridge_tui._command_completions("/task a")
    assert bridge_tui._strip_slash("/pause now") == "pause now"
    assert bridge_tui._strip_slash("/?") == "?" and bridge_tui._strip_slash("/") == "/" and bridge_tui._strip_slash("/9") == "/9"
    assert bridge_tui._confirmation_reason("/cancel") == bridge_tui._confirmation_reason("cancel")
    assert bridge_tui._confirmation_reason("/cancel")

    calls: list[str] = []
    state = bridge_tui._ConsoleState("M", lambda _m, command: calls.append(command) or "ok", lambda *_: {}, full=True)
    for char in "/pa":
        state.handle_key(char)
    state.handle_key("\t")
    assert state.command == "/pause"
    state.handle_key("KEY_ENTER")
    deadline = time.monotonic() + 2
    while state.command_pending and time.monotonic() < deadline:
        state.refresh()
        time.sleep(0.01)
    assert calls == ["pause"] and list(state.history) == ["/pause"]  # history keeps what was typed
    state.command = "/cancel"
    state.command_cursor = len(state.command)
    state.handle_key("KEY_ENTER")
    assert state.confirm_command == "/cancel" and calls == ["pause"]
    state.handle_key("n")
    state._dispatch("/help")
    assert state.document_viewer is not None and "help" in state.document_viewer.title.lower()


def test_slash_in_palette_still_edits_the_filter() -> None:
    state = make_state(full_snapshot(), view="Actions")
    state.handle_key("/")
    assert state.palette_filter_editing and state.command == ""
    for char in "1":
        state.handle_key(char)
    state.handle_key("KEY_ENTER")
    assert [action.label for action in state.palette_actions()] == ["Approve task 1", "Reject task 1"]
    assert state.command == ""


# --- every view at every supported width -------------------------------------------------------

@pytest.mark.parametrize("width", [60, 80, 120])
@pytest.mark.parametrize("full", [True, False])
def test_all_views_fit_without_overflow_or_collapsed_spacing(width: int, full: bool, monkeypatch) -> None:
    snapshot = full_snapshot()
    snapshot["events"] = [event("task-added", {"title": "A"}, task_id=1), event("controller-heartbeat", {}), event("controller-heartbeat", {})]
    snapshot["operations"] = [{"id": "0123456789ab", "task_id": 1, "station": "codex", "model": "gpt", "status": "EXITED", "native_session_id": "s" * 36, "restoration_state": "RESTORED_NATIVE"}]
    snapshot["stations"] = [{"id": "codex", "name": "codex", "model": "m", "status": "configured"}]
    snapshot["usage"] = {"status": "ready", "age_seconds": 1, "text": "Provider  Scope  " + "x" * 150}
    state = make_state(snapshot, full=full)
    height = 34 if full else 6
    for index, view in enumerate(bridge_tui.VIEWS):
        state.choose_view(index)
        screen = Screen(height, width)
        (bridge_tui._draw_full if full else bridge_tui._draw_compact)(screen, state)  # type: ignore[arg-type]
        assert all(cell_width(line) <= width for line in screen.lines), view
        assert screen.lines[0].startswith("LLM BRIDGE · ▌ Fix the typo · ● RUNNING"), view
        assert "LLM BRIDGE Fix" not in screen.lines[0]
        assert screen.lines[2].lstrip().startswith(("1 Bridge", "< ", "[1 Bridge]")) or "[" in screen.lines[2]
        assert screen.lines[height - 1].rstrip().endswith("? help"), (view, screen.lines[height - 1])
        assert screen.lines[height - 2].startswith("› "), view


def test_no_color_rendering_keeps_every_cue_in_text(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert not bridge_tui.initialize_colors(is_tty=True)
    state = make_state(full_snapshot())
    screen = Screen(34, 100)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert "RUNNING" in screen.lines[0] and "Needs you" in "\n".join(screen.lines)
    attributes = (bridge_tui.curses.A_DIM | bridge_tui.curses.A_BOLD | bridge_tui.curses.A_REVERSE
                  | bridge_tui.curses.A_UNDERLINE)
    assert all(attr & ~attributes == 0 for _y, _x, _text, attr in screen.writes)  # attributes only, never color pairs


# --- Orders master-detail and Bridge recent activity -----------------------------

def orders_snapshot() -> dict[str, object]:
    snapshot = full_snapshot()
    snapshot["tasks"] = [
        {"id": 2, "title": "Rewrite the quick start", "status": "QUEUED", "priority": 5, "revision": 3,
         "depends_on": [1], "acceptance": ["installs in five minutes", "links resolve"],
         "evidence": ["pytest: 12 passed", "docs build ok"],
         "prompt": "# Quick start\n\nRewrite the README.\n\n## Notes\nKeep it short."},
        {"id": 10, "title": "Troubleshooting", "status": "VERIFYING", "priority": 12, "prompt": "Add a section."},
    ]
    return snapshot


def test_order_list_rows_align_id_state_and_priority() -> None:
    tasks = bridge_tui._items(orders_snapshot()["tasks"])
    assert bridge_tui._order_list_rows(tasks) == [
        "2   ○ QUEUED     P5   after 1  Rewrite the quick start",
        "10  ◆ VERIFYING  P12           Troubleshooting",
    ]
    assert bridge_tui._order_list_rows([{"id": 1, "status": "queued", "title": "No priority"}]) == ["1  ○ QUEUED  No priority"]


def test_orders_list_height_keeps_room_for_the_preview() -> None:
    assert bridge_tui._orders_list_height(24, 110, 3) == 3
    assert bridge_tui._orders_list_height(24, 110, 40) == 15  # capped: separator + 8 preview rows remain
    assert bridge_tui._orders_list_height(12, 110, 3) == 3 and bridge_tui._orders_list_height(11, 110, 2) == 2
    assert bridge_tui._orders_list_height(11, 110, 3) is None and bridge_tui._orders_list_height(11, 110, 40) is None
    assert bridge_tui._orders_list_height(24, 59, 3) is None  # too narrow
    assert bridge_tui._orders_list_height(24, 110, 0) is None


def test_order_preview_lists_fields_and_plain_prompt() -> None:
    task = bridge_tui._items(bridge_tui._bounded_snapshot(orders_snapshot())["tasks"])[0]
    rows = bridge_tui._order_preview_rows(task, 80, 20)
    assert rows[0] == "Revision    3"  # the title is in the list row and the separator, not repeated
    assert not any(row.startswith("Title") for row in rows)
    assert "Depends on  1" in rows
    assert "Acceptance  1. installs in five minutes" in rows and "            2. links resolve" in rows
    assert "Evidence    2 items · latest: docs build ok" in rows
    prompt = rows[rows.index(next(row for row in rows if row.startswith("Prompt"))):]
    assert prompt == ["Prompt      Quick start", "            Rewrite the README.", "            Notes", "            Keep it short."]
    separator = bridge_tui._order_separator(task, 40)
    assert separator.startswith("── task 2 · ○ QUEUED · priority 5 ──") and cell_width(separator) == 40
    assert bridge_tui._order_preview_rows(task, 80, 0) == []
    # The fixed fields win over the prompt when space is short; a cut prompt ends in an ellipsis.
    short = bridge_tui._order_preview_rows(task, 80, 8)
    assert len(short) == 8 and short[-3].startswith("Prompt") and short[-1].endswith("…")
    bare = bridge_tui._order_preview_rows({"id": 3, "title": "", "acceptance": [], "evidence": []}, 80, 10)
    assert bare == ["Acceptance  none recorded", "Evidence    none yet"]
    many = bridge_tui._order_preview_rows({"title": "T", "acceptance": [f"c{n}" for n in range(8)]}, 80, 20)
    assert "            +3 more" in many


def test_orders_view_shows_selected_task_preview_below_the_list() -> None:
    state = make_state(orders_snapshot(), view="Tasks")
    screen = Screen(30, 110)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[3].startswith("> 2   ○ QUEUED     P5   after 1  Rewrite the quick start")
    assert screen.lines[4].startswith("  10  ◆ VERIFYING  P12           Troubleshooting")
    assert screen.lines[5].startswith("  ── task 2 · ○ QUEUED · priority 5 ──")
    assert any(line.strip().startswith("Acceptance  1. installs in five minutes") for line in screen.lines)
    assert any(line.strip().startswith("Prompt      Quick start") for line in screen.lines)
    state.handle_key("KEY_DOWN")  # the preview follows the selection
    screen = Screen(30, 110)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert screen.lines[4].startswith("> 10  ◆ VERIFYING")
    assert screen.lines[5].startswith("  ── task 10 · ◆ VERIFYING · priority 12 ──")
    assert any("Prompt      Add a section." in line for line in screen.lines)
    assert not any("Quick start" in line for line in screen.lines)
    # Clicks on the separator and preview rows do not move the selection; list rows still select.
    state.handle_mouse(8, 10, 30, 110)
    assert state.selected == 1
    state.handle_mouse(3, 10, 30, 110)
    assert state.selected == 0


@pytest.mark.parametrize("height,width", [(30, 50), (12, 110)])
def test_orders_view_keeps_plain_list_when_terminal_is_small(height: int, width: int) -> None:
    state = make_state(orders_snapshot(), view="Tasks")
    screen = Screen(height, width)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert not any("── task" in line for line in screen.lines)
    assert screen.lines[3].startswith("> 2")


def test_orders_inspection_replaces_the_preview() -> None:
    state = make_state(orders_snapshot(), view="Tasks")
    state.handle_key("\n")
    screen = Screen(30, 110)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    assert not any("── task" in line for line in screen.lines)
    assert any(line.lstrip("> ").startswith("Task 2: Rewrite the quick start") for line in screen.lines)


def test_bridge_overview_fills_spare_rows_with_recent_activity() -> None:
    snapshot = full_snapshot()
    snapshot["events"] = [
        event("task-added", {"title": "A"}, task_id=1),
        event("controller-heartbeat", {"controller_state": "ACTIVE"}),
        event("controller-heartbeat", {"controller_state": "ACTIVE"}),
        event("task-status", {"from": "QUEUED", "to": "RUNNING"}, task_id=1),
        event("operation-running", {"route_id": "codex", "model": "gpt-5"}, task_id=1),
    ]
    state = make_state(snapshot)
    screen = Screen(34, 110)
    bridge_tui._draw_full(screen, state)  # type: ignore[arg-type]
    recent = [line.strip() for line in screen.lines if "Task 1 added" in line or "Started codex" in line or "controller alive" in line]
    # Only operator-level events: no heartbeats, no task-status RUNNING bookkeeping.
    assert len(recent) == 2 and recent[0].endswith("Task 1 added · A") and recent[1].endswith("Started codex (gpt-5) on task 1")
    first = next(line for line in screen.lines if "Recent" in line)
    assert first.startswith("  Recent      ") and "Task 1 added" in first
    tight = Screen(14, 110)  # the overview fills the content rows, leaving none spare
    bridge_tui._draw_full(tight, state)  # type: ignore[arg-type]
    assert not any("Recent" in line for line in tight.lines)
    state.event_offset = 20  # older pages belong to the Log view
    paged = Screen(34, 110)
    bridge_tui._draw_full(paged, state)  # type: ignore[arg-type]
    assert not any("Recent" in line for line in paged.lines)
    assert bridge_tui._recent_event_rows([], 80, 5) == []
    assert bridge_tui._recent_event_rows(snapshot["events"], 80, 0) == []  # type: ignore[arg-type]
