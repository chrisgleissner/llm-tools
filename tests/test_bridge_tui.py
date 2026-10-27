from __future__ import annotations

import time
import shlex
import os
import stat
import subprocess
from pathlib import Path

from llm_tools import bridge_tui


class FakeWindow:
    def __init__(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.lines = [""] * height

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.lines = [""] * self.height

    def addnstr(self, y: int, x: int, text: str, count: int) -> None:
        current = self.lines[y]
        self.lines[y] = (current[:x] + text[:count]).ljust(self.width)[: self.width]

    def noutrefresh(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def keypad(self, _enabled: bool) -> None:
        pass

    def timeout(self, _milliseconds: int) -> None:
        pass


def _wait_for(predicate, state: bridge_tui._ConsoleState, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state.refresh()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_console_state_dispatches_through_injected_facade_and_refreshes() -> None:
    calls: list[tuple[str, str]] = []
    snapshots: list[str] = []

    def snapshot(mission_id: str) -> dict[str, object]:
        snapshots.append(mission_id)
        return {
            "mission": {"id": mission_id, "status": "RUNNING", "objective": "build it"},
            "tasks": [{"id": "T1", "title": "First", "status": "READY"}],
            "task_counts": {"READY": 1},
        }

    def dispatch(mission_id: str, command: str) -> str:
        calls.append((mission_id, command))
        return "queued command"

    state = bridge_tui._ConsoleState("M1", dispatch, snapshot, full=False)
    state.refresh(force=True)
    _wait_for(lambda: bool(state.snapshot), state)
    assert state.rows()[0] == "Mission: M1 [RUNNING]"
    assert snapshots == ["M1"]
    last_refresh = state.last_refresh
    state.refresh(now=last_refresh + 0.1)
    assert snapshots == ["M1"]  # Refresh stays at or below four calls per second.

    for char in "status":
        state.handle_key(char)
    state.handle_key("KEY_ENTER")
    _wait_for(lambda: not state.command_pending and len(snapshots) == 2, state)
    assert calls == [("M1", "status")]
    assert state.command == ""
    assert state.output[-1] == "queued command"
    assert len(snapshots) == 2  # A command completion forces an immediate refresh.


def test_command_feedback_summarizes_durable_envelopes_and_preserves_plain_results() -> None:
    assert bridge_tui._command_feedback(
        "priority 42 7",
        '{"id":"secret-command-uuid","status":"APPLIED","result":{"task_id":42,"priority":7}}',
    ) == "Applied: priority 42 7"
    assert bridge_tui._command_feedback(
        "verify 42",
        '{"status":"REJECTED","result":{"error":"task is not ready"}}',
    ) == "Rejected: task is not ready"
    assert bridge_tui._command_feedback("status", "human-readable status") == "human-readable status"


def test_command_feedback_keeps_help_and_json_results_multiline_and_bounded() -> None:
    help_text = "usage: task add\n  --prompt TEXT\n  --acceptance CHECK\n"
    assert bridge_tui._command_feedback("task add --help", help_text) == help_text.rstrip("\n")

    json_text = bridge_tui._command_feedback("task list", '{"tasks":[{"id":1,"title":"first"}]}')
    assert json_text.startswith("{\n")
    assert '\n      "id": 1,' in json_text

    too_many_lines = "\n".join(f"option-{index}" for index in range(bridge_tui._MAX_RESPONSE_LINES + 4))
    bounded = bridge_tui._command_feedback("help", too_many_lines)
    assert len(bounded.splitlines()) == bridge_tui._MAX_RESPONSE_LINES
    assert bounded.splitlines()[-1] == "[response truncated to fit the Bridge console limit]"
    assert "\x1b" not in bridge_tui._command_feedback("help", "\x1b[2Jsafe\x1b[0m")


def test_console_keyboard_mouse_views_and_palette_share_dispatch() -> None:
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M2", lambda _mission, cmd: calls.append(cmd) or "ok", lambda _id: {}, full=True)

    state.handle_key("KEY_F3")
    assert state.view == "Stations"
    state.handle_key("\t")
    assert state.view == "Systems"
    nav = bridge_tui._nav_line(state.view_index, "  ")
    stations_x = nav.index("Stations") + 2
    state.handle_mouse(2, stations_x, 24, 80)
    assert state.view == "Stations"
    state.handle_mouse(2, 78, 24, 80)
    assert state.view == "Usage"
    state.handle_key("KEY_F7")
    state.handle_mouse(3, 2, 24, 80)
    assert state.command == "help"  # A click stages an action; Enter runs it.
    state.handle_key("\x15")
    state.selected = 4
    state.handle_key("KEY_ENTER")
    _wait_for(lambda: not state.command_pending, state)
    assert calls == ["pause"]
    assert bridge_tui.PALETTE_COMMANDS[8:11] == ("steer --help", "attach", "detach")
    assert bridge_tui.PALETTE_COMMANDS[11:] == ("usage", "control", "task add --help", "proposal list")


def test_multiline_response_overlay_scrolls_and_view_keys_dismiss_without_dispatching_text() -> None:
    calls: list[str] = []
    state = bridge_tui._ConsoleState(
        "M2",
        lambda _mission, cmd: calls.append(cmd) or "usage: task add\n" + "\n".join(f"--option-{n}" for n in range(30)),
        lambda _id: {},
        full=True,
    )
    state._dispatch("task add --help")
    _wait_for(lambda: not state.command_pending and state.response_overlay, state)
    assert state.expand_requested
    assert len(state.output) > 20
    state.response_page_size = 5
    state.handle_key("KEY_NPAGE")
    assert state.response_scroll == 5
    state.handle_key("KEY_UP")
    assert state.response_scroll == 4
    state.handle_key("x")
    assert state.command == ""
    state.handle_key("KEY_F3")
    assert not state.response_overlay and state.view == "Stations"
    state.handle_key("\n")
    assert calls == ["task add --help"]


def test_response_overlay_renders_wrapped_scrolled_and_empty_output(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    state = bridge_tui._ConsoleState("M2", lambda _mission, _command: "", lambda _id: {}, full=True)
    state.output.extend([f"--option-{index}" for index in range(12)])
    state.response_scroll = 100
    window = FakeWindow(8, 16)
    bridge_tui._draw_response_overlay(window, state)  # type: ignore[arg-type]
    assert "Command response" in window.lines[0]
    assert "--option-11" in "\n".join(window.lines)
    assert window.lines[-1].startswith("8-12/12 response")
    assert state.response_scroll < 100
    state.output.clear()
    assert bridge_tui._response_rows(state, 20) == ["No command response."]


def test_full_view_mouse_selects_the_task_row_under_the_pointer() -> None:
    state = bridge_tui._ConsoleState(
        "M2",
        lambda _mission, _command: "",
        lambda _id: {},
        full=True,
        snapshot={
            "tasks": [
                {"id": "A", "title": "First", "status": "READY"},
                {"id": "B", "title": "Second", "status": "QUEUED"},
            ]
        },
    )
    state.choose_view(1)
    state.handle_mouse(4, 10, 24, 80)
    assert state.selected == 1
    assert state.rows()[state.selected].startswith("B  QUEUED")


def test_station_view_distinguishes_viewed_from_executing_and_enter_selects_view() -> None:
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M2", lambda _mission, command: calls.append(command) or "saved", lambda _id: {}, full=True)
    state.snapshot = bridge_tui._bounded_snapshot({
        "stations": [
            {"id": "codex", "name": "codex", "model": "gpt-5", "status": "configured"},
            {"id": "claude", "name": "claude", "model": "sonnet", "status": "configured"},
            {"id": "kilo-zai", "name": "kilo-zai", "model": "zai/glm-5.2", "observed_model": "zai/glm-5.2", "status": "configured"},
        ],
        "station_config": {"mode": "native", "providers": ["codex", "claude"], "routes": ["kilo-zai"]},
        "station_observation": {"station": "kilo-zai", "model": "zai/glm-5.2", "effort": "unknown", "operation_status": "RUNNING"},
        "station_context": {"executing_operation": "operation-7", "viewed_station": "claude", "provider_liveness": "unknown", "operation_status": "RUNNING", "last_launched_station": "kilo-zai"},
        "station_mode": {"configured_mode": "native", "actual_mode": "unknown", "source": "unavailable"},
        "capacity": {"source": "provider-selection event", "selection": {"provider": "kilo", "route": "kilo-zai", "selected_model": "zai/glm-5.2"}},
        "lifecycle": {
            "status": "RUNNING",
            "controller": "healthy",
            "heartbeat": {
                "reported_state": "ACTIVE",
                "observed_at": "2026-10-25T20:00:00Z",
                "age_seconds": 8,
                "owner_lock_held": True,
                "desired_mission_status": "RUNNING",
                "current_liveness": "unknown",
                "source": "controller-heartbeat",
                "unbounded_payload": {"token": "must not project"},
            },
        },
        "health": {"status": "ok"},
    })
    state.choose_view(2)

    rows = state.rows()
    assert "Viewed: claude | last launched: kilo-zai" in rows[0]
    assert "operation-7" in rows[0]
    assert "Provider liveness: unknown" in rows[1]
    assert "VIEWED" in rows[3]
    assert "LAST LAUNCHED" in rows[4]

    state.selected = 1
    state.handle_key("KEY_ENTER")
    _wait_for(lambda: not state.command_pending, state)
    assert calls == ["station claude"]

    state.choose_view(3)
    system_rows = "\n".join(state.rows())
    assert "capacity:" in system_rows and "selected_model=zai/glm-5.2" in system_rows
    assert "configured_mode=native" in system_rows and "actual_mode=unknown" in system_rows
    assert "Controller heartbeat (last observed):" in system_rows
    assert "age_seconds=8" in system_rows and "current_liveness=unknown" in system_rows
    assert "source=controller-heartbeat" in system_rows
    assert "unbounded_payload" not in system_rows


def test_sessions_view_labels_operation_model_and_native_identity_separately() -> None:
    state = bridge_tui._ConsoleState("M10", lambda _mission, _command: "", lambda _id: {}, full=True)
    state.snapshot = bridge_tui._bounded_snapshot({
        "operations": [{
            "id": "op-123",
            "task_id": 9,
            "station": "claude",
            "route_id": "claude-sonnet",
            "model": "sonnet",
            "status": "EXITED",
            "native_session_id": "session-uuid-1234",
            "restoration_state": "RESTORED_NATIVE",
        }],
    })
    state.choose_view(5)

    row = state.rows()[0]
    assert "op op-123" in row
    assert "pinned model sonnet" in row
    assert "native session session-uuid-1234 restoration RESTORED_NATIVE" in row


def test_orders_enter_inspects_task_details_and_ctrl_r_editor_starts_with_prompt(monkeypatch) -> None:
    calls: list[str] = []
    sent_prompt: list[str] = []
    editor_initial: list[str] = []

    def run_editor(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        path = Path(argv[-1])
        editor_initial.append(path.read_text(encoding="utf-8"))
        path.write_text(path.read_text(encoding="utf-8") + "\nrevised", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(bridge_tui.subprocess, "run", run_editor)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)
    snapshot = bridge_tui._bounded_snapshot({
        "tasks": [{
            "id": 41, "title": "inspect order", "prompt": "original prompt\nsecond line",
            "status": "READY", "priority": 9, "revision": 3, "depends_on": [12],
            "acceptance": ["tests pass"], "evidence": ["pytest output"],
            "pending": {"title": "pending title", "priority": 10},
            "amendments": [{"revision": 3, "status": "PENDING", "changes": {"title": "pending title"}, "secret": "excluded"}],
            "unbounded_payload": "do not retain",
        }],
    })
    def dispatch(_mission: str, line: str) -> str:
        if shlex.split(line) == ["task", "show", "--task", "41"]:
            import json
            return json.dumps({"id":41,"prompt":"original prompt\nsecond line"})
        calls.append(line)
        sent_prompt.append(Path(shlex.split(line)[-1]).read_text(encoding="utf-8"))
        return "amended"

    state = bridge_tui._ConsoleState("M11", dispatch, lambda _id: {}, full=True, snapshot=snapshot)
    state.choose_view(1)
    state.handle_key("KEY_ENTER")
    detail = "\n".join(state.rows())
    assert "priority 9" in detail and "revision 3" in detail
    assert "Dependencies: 12" in detail
    assert "tests pass" in detail and "pytest output" in detail
    assert "Pending amendment" in detail and "pending title" in detail
    assert "Amendment history:" in detail and "revision 3 PENDING" in detail
    assert "original prompt" in detail
    assert "unbounded_payload" not in str(state.snapshot)

    task = state.selected_task()
    assert task is not None
    bridge_tui._edit_task_prompt(FakeWindow(24, 80), state, "41", initial_text=str(task["prompt"]))  # type: ignore[arg-type]
    _wait_for(lambda: not state.command_pending, state)
    assert editor_initial == ["original prompt\nsecond line"]
    assert calls and calls[0].startswith("amend 41 ")
    assert sent_prompt == ["original prompt\nsecond line\nrevised"]


def test_orders_detail_scroll_does_not_change_selected_order_or_amend_target() -> None:
    snapshot = bridge_tui._bounded_snapshot({
        "tasks": [
            {"id": 11, "title": "first", "prompt": "first prompt", "status": "READY"},
            {"id": 22, "title": "second", "prompt": "second prompt", "status": "READY"},
        ],
    })
    state = bridge_tui._ConsoleState("M11", lambda _mission, _line: "", lambda _id: {}, full=True, snapshot=snapshot)
    state.choose_view(1)
    state.handle_key("KEY_DOWN")
    assert state.selected == 1
    state.handle_key("KEY_ENTER")
    state.handle_key("KEY_DOWN")
    state.handle_key("KEY_NPAGE")
    assert state.detail_scroll == 11
    assert state.selected == 1
    assert bridge_tui._selected_task_id(state) == "22"
    assert "second prompt" in state.rows()[-1]

    state.handle_mouse(3, 5, 24, 80)
    assert state.selected == 1
    assert state.detail_scroll == 0
    state.handle_key("\x1b")
    assert state.task_detail is False


def test_systems_preserves_and_renders_quota_budget_and_reset_windows() -> None:
    snapshot = bridge_tui._bounded_snapshot({
        "capacity": {
            "source": "persisted runtime event",
            "selection": {
                "provider": "codex", "route": "codex", "selected_model": "gpt-6.1-sol",
                "decision": {"usable": False, "reason": "rate-limited"},
                "decisions": [
                    {"provider": "codex", "windows": [
                        {"name": "weekly", "kind": "reset_window", "remaining_percent": 34.0, "reset_epoch": 1999999999, "reason": "cooldown"},
                        {"name": "balance", "kind": "budget", "remaining_amount": 12.5, "currency": "USD", "reset_epoch": None},
                    ]},
                ],
                "private_payload": {"token": "excluded"},
            },
        },
    })
    state = bridge_tui._ConsoleState("M12", lambda _mission, _line: "", lambda _id: {}, full=True, snapshot=snapshot)
    state.choose_view(3)
    rendered = "\n".join(state.rows())
    assert "Quota/budget codex weekly [reset_window]: remaining 34.0%; est. runout unknown; reset epoch 1999999999" in rendered
    assert "Quota/budget codex balance [budget]: remaining 12.5 USD; no reset" in rendered
    assert "private_payload" not in str(state.snapshot)


def test_stations_enter_selects_view_and_shows_recent_operation_history() -> None:
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M13", lambda _mission, line: calls.append(line) or "saved", lambda _id: {}, full=True, snapshot=bridge_tui._bounded_snapshot({
        "stations": [{"id": "codex", "name": "codex", "model": "gpt-6.1-sol", "status": "configured"}],
        "station_context": {"viewed_station": "unknown", "last_launched_station": "codex"},
        "operations": [{"id": "op-5", "task_id": 6, "station": "codex", "route_id": "codex", "model": "gpt-6.1-sol", "status": "EXITED", "exit_code": 0}],
    }))
    state.choose_view(2)
    rendered = "\n".join(state.rows())
    assert "Recent history for codex" in rendered
    assert "op op-5 task 6 EXITED model gpt-6.1-sol exit 0" in rendered
    state.handle_key("KEY_ENTER")
    _wait_for(lambda: not state.command_pending, state)
    assert calls == ["station codex"]


def test_real_monitor_projection_survives_tui_bounding_and_systems_render(tmp_path) -> None:
    from llm_tools.bridge_monitor import project_snapshot
    from llm_tools.bridge_store import BridgeStore

    with BridgeStore(tmp_path / "monitor-ui.sqlite3") as store:
        mission = store.create_mission("monitor UI", "inspect persisted system data", cwd=tmp_path, config={
            "headless": True,
            "providers": "codex",
            "frozen_tool_config": '[providers.codex]\nmodel = "gpt-6.1-sol"\n',
        })
        task_id = store.add_task(mission["id"], "work")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.mark_operation_running(operation, process_pid=123, process_start="test-start")
        store.record_runtime_event(mission["id"], "controller-heartbeat", {
            "controller_state": "RUNNING", "observed_at": "2026-10-26T13:00:00Z",
            "owner_lock_held": True, "desired_mission_status": "RUNNING",
        })
        store.record_runtime_event(mission["id"], "controller-mode", {"verified": True, "actual_mode": "headless"})
        monitor_data = project_snapshot(store, mission["id"], task_limit=20)

    state = bridge_tui._ConsoleState(
        mission["id"], lambda _mission, _line: "", lambda _id: {},
        full=True, snapshot=bridge_tui._bounded_snapshot(monitor_data),
    )
    state.choose_view(2)
    station_rows = "\n".join(state.rows())
    assert "codex  configured gpt-6.1-sol  observed unknown" in station_rows
    assert "Provider liveness: unknown" in station_rows
    state.choose_view(3)
    system_rows = "\n".join(state.rows())
    assert "configured_mode=headless" in system_rows and "actual_mode=headless" in system_rows
    assert "Controller heartbeat (last observed):" in system_rows
    assert "current_liveness=unknown" in system_rows


def test_mouse_double_click_uses_enter_activation_for_orders_and_stations() -> None:
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M14", lambda _mission, line: calls.append(line) or "ok", lambda _id: {}, full=True, snapshot=bridge_tui._bounded_snapshot({
        "tasks": [{"id": 8, "title": "mouse order", "status": "READY", "prompt": "prompt"}],
        "stations": [{"id": "codex", "name": "codex", "model": "configured", "status": "configured"}],
    }))
    state.choose_view(1)
    bridge_tui._handle_mouse_event(state, 3, 5, 24, 80, activate=True)
    assert state.task_detail is True
    state.choose_view(2)
    bridge_tui._handle_mouse_event(state, 5, 5, 24, 80, activate=True)
    _wait_for(lambda: not state.command_pending, state)
    assert calls == ["station codex"]


def test_run_control_loop_maps_f_keys_enter_escape_and_exit(monkeypatch) -> None:
    window = FakeWindow(24, 80)
    keys = iter([bridge_tui.curses.KEY_F2, bridge_tui.curses.KEY_ENTER, "\x1b", "\x04"])
    window.get_wch = lambda: next(keys)  # type: ignore[attr-defined]
    monkeypatch.setattr(bridge_tui.curses, "wrapper", lambda callback: callback(window))
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "curs_set", lambda _value: None)
    monkeypatch.setattr(bridge_tui.curses, "mousemask", lambda _mask: None)
    result = bridge_tui.run_control(
        "M15",
        lambda _mission, _line: "unused",
        lambda mission, **_kwargs: {"mission": {"id": mission}, "tasks": [{"id": 2, "title": "inspect", "status": "READY", "prompt": "p"}]},
    )
    assert result == 0


def test_tui_compact_and_full_render_at_supported_sizes(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    state = bridge_tui._ConsoleState(
        "M3",
        lambda _mission, _command: "",
        lambda _mission: {"mission": {"name": "Build", "status": "PAUSED", "objective": "ship this"}},
        full=False,
    )
    state.refresh(force=True)
    _wait_for(lambda: bool(state.snapshot), state)
    compact = FakeWindow(6, 80)
    bridge_tui._draw_compact(compact, state)  # type: ignore[arg-type]
    assert "LLM BRIDGE" in compact.lines[0]
    assert "[1 Bridge]" in compact.lines[2]
    assert compact.lines[4].startswith("> ")
    assert "Ctrl-D exit" in compact.lines[5]

    state.full = True
    full = FakeWindow(24, 80)
    bridge_tui._draw_full(full, state)  # type: ignore[arg-type]
    assert "Bridge" in full.lines[2]
    assert full.lines[22].startswith("> ")
    assert len(full.lines) == 24


def test_snapshot_errors_and_terminal_controls_are_rendered_safely() -> None:
    state = bridge_tui._ConsoleState(
        "M4",
        lambda _mission, _command: "\x1b[31msecret-like output\x1b[0m",
        lambda _mission: (_ for _ in ()).throw(RuntimeError("read\x1b[2J failed")),
        full=False,
    )
    state.refresh(force=True)
    _wait_for(lambda: bool(state.snapshot_error), state)
    state.handle_key("s")
    state.handle_key("\n")
    _wait_for(lambda: not state.command_pending, state)
    assert "\x1b" not in state.output[-1]


def test_console_retains_only_bounded_task_projection() -> None:
    tasks = [
        {"id": index, "title": f"task {index}", "status": "QUEUED", "original_prompt": "p" * 10000}
        for index in range(10000)
    ]
    state = bridge_tui._ConsoleState(
        "M5",
        lambda _mission, _command: "",
        lambda _mission: {"mission": {"id": "M5"}, "tasks": tasks, "task_counts": {"QUEUED": 10000}},
        full=True,
    )
    state.refresh(force=True)
    _wait_for(lambda: bool(state.snapshot), state)
    assert len(state.snapshot["tasks"]) == 200  # type: ignore[arg-type]
    assert "original_prompt" not in state.snapshot["tasks"][0]  # type: ignore[index]
    state.choose_view(1)
    assert len(state.rows()) == 200


def test_orders_page_through_large_missions_with_bounded_snapshot_reads() -> None:
    offsets: list[int] = []

    def snapshot(_mission: str, *, task_offset: int, task_limit: int) -> dict[str, object]:
        offsets.append(task_offset)
        end = min(450, task_offset + task_limit)
        return {
            "mission": {"id": "M8", "status": "RUNNING"},
            "tasks": [
                {"id": index + 1, "title": f"task {index + 1}", "status": "QUEUED"}
                for index in range(task_offset, end)
            ],
            "task_offset": task_offset,
            "task_total": 450,
            "task_counts": {"QUEUED": 450},
        }

    state = bridge_tui._ConsoleState("M8", lambda _mission, _command: "", snapshot, full=True)
    state.choose_view(1)
    state.refresh(force=True)
    _wait_for(lambda: len(state.snapshot.get("tasks", [])) == 200, state)
    assert state.rows()[0].startswith("1  QUEUED")

    state.handle_key("KEY_NPAGE")
    _wait_for(lambda: offsets[-1:] == [200] and state.rows()[0].startswith("201  QUEUED"), state)
    assert len(state.snapshot["tasks"]) == 200  # type: ignore[arg-type]

    state.handle_key("KEY_NPAGE")
    _wait_for(lambda: offsets[-1:] == [400] and state.rows()[0].startswith("401  QUEUED"), state)
    assert len(state.snapshot["tasks"]) == 50  # type: ignore[arg-type]
    state.handle_key("KEY_NPAGE")
    assert state.task_offset == 400  # A partial final page cannot advance past the queue.

    state.handle_key("KEY_PPAGE")
    _wait_for(lambda: offsets[-1:] == [200] and state.rows()[0].startswith("201  QUEUED"), state)


def test_orders_search_filters_all_tasks_without_dispatching_a_command() -> None:
    searches: list[str] = []
    dispatches: list[str] = []

    def snapshot(_mission: str, *, task_offset: int, task_limit: int, search: str) -> dict[str, object]:
        searches.append(search)
        tasks = [
            {"id": index, "title": f"task-{index}", "status": "QUEUED"}
            for index in range(1, 501)
            if not search or search in f"task-{index}"
        ]
        return {
            "mission": {"id": "M9", "status": "RUNNING"},
            "tasks": tasks[task_offset : task_offset + task_limit],
            "task_total": len(tasks),
            "task_counts": {"QUEUED": len(tasks)},
        }

    state = bridge_tui._ConsoleState(
        "M9",
        lambda _mission, command: dispatches.append(command) or "unexpected",
        snapshot,
        full=True,
    )
    state.choose_view(1)
    state.refresh(force=True)
    _wait_for(lambda: state.snapshot.get("task_total") == 500, state)

    state.handle_key("\x06")
    for char in "task-500":
        state.handle_key(char)
    state.handle_key("KEY_ENTER")
    _wait_for(lambda: searches[-1:] == ["task-500"] and state.snapshot.get("task_total") == 1, state)
    assert state.rows()[0].startswith("500  QUEUED")
    assert dispatches == []

    state.handle_key("\x15")
    _wait_for(lambda: searches[-1:] == [""] and state.snapshot.get("task_total") == 500, state)
    assert len(state.snapshot["tasks"]) == 200  # type: ignore[arg-type]


def test_console_printable_commands_are_never_navigation_shortcuts() -> None:
    expected = (
        "help",
        "verify 123 --approve",
        "priority 1 2",
        "add /tmp/task file.md",
        "amend 17 /tmp/amendment.md",
    )
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M6", lambda _mission, command: calls.append(command) or "ok", lambda _id: {}, full=False)
    for command in expected:
        for char in command:
            state.handle_key(char)
        assert state.command == command
        state.handle_key("KEY_ENTER")
        _wait_for(lambda: not state.command_pending, state)
    assert calls == list(expected)
    state.handle_key("\x10")
    assert state.command == expected[-1]
    state.handle_key("\x0e")
    assert state.command == ""

    state.handle_key("KEY_F4")
    assert state.view == "Systems"


def test_slow_snapshot_and_dispatch_never_block_keyboard_input() -> None:
    def slow_snapshot(_mission: str) -> dict[str, object]:
        time.sleep(0.4)
        return {"mission": {"id": "slow"}}

    def slow_dispatch(_mission: str, command: str) -> str:
        time.sleep(0.4)
        return command

    state = bridge_tui._ConsoleState("slow", slow_dispatch, slow_snapshot, full=False)
    started = time.monotonic()
    state.refresh(force=True)
    for char in "help":
        state.handle_key(char)
    state.handle_key("KEY_ENTER")
    assert time.monotonic() - started < 0.25
    assert state.command_pending

    state.output.extend(str(index) for index in range(1000))
    assert len(state.output) == bridge_tui._MAX_OUTPUT_LINES
    _wait_for(lambda: bool(state.snapshot) and not state.command_pending, state, timeout=1.5)
    assert state.output[-1] == "help"


def test_private_editor_file_dispatches_multiline_task_and_cleans_up(monkeypatch) -> None:
    calls: list[tuple[list[str], str]] = []
    editor_argv: list[str] = []

    def run_editor(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        editor_argv.extend(argv)
        path = argv[-1]
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        Path(path).write_text("first line\nsecond line\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("VISUAL", "fake-editor --wait")
    monkeypatch.setattr(bridge_tui.subprocess, "run", run_editor)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)

    def dispatch(_mission: str, command: str) -> str:
        argv = shlex.split(command)
        calls.append((argv, Path(argv[-1]).read_text(encoding="utf-8")))
        return "task queued"

    state = bridge_tui._ConsoleState("M7", dispatch, lambda _id: {}, full=False)
    bridge_tui._edit_task_prompt(FakeWindow(6, 80), state)  # type: ignore[arg-type]
    _wait_for(lambda: not state.command_pending, state)
    assert editor_argv[:2] == ["fake-editor", "--wait"]
    assert calls == [(["add", editor_argv[-1]], "first line\nsecond line\n")]
    assert not Path(editor_argv[-1]).exists()
    assert state.output[-1] == "task queued"


def test_editor_appends_file_to_typed_task_options(monkeypatch) -> None:
    calls: list[list[str]] = []

    def run_editor(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        Path(argv[-1]).write_text("multiline prompt\nsecond line", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(bridge_tui.subprocess, "run", run_editor)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)
    state = bridge_tui._ConsoleState("M7", lambda _mission, line: calls.append(shlex.split(line)) or "queued", lambda _id: {}, full=False)
    state.command = "task add --priority 5 --acceptance 'tests pass'"
    bridge_tui._edit_task_prompt(FakeWindow(6, 80), state)  # type: ignore[arg-type]
    _wait_for(lambda: not state.command_pending, state)
    assert calls and calls[0][:7] == ["task", "add", "--priority", "5", "--acceptance", "tests pass", "--file"]
    assert Path(calls[0][-1]).exists() is False
    assert state.command == ""


def test_editor_amends_selected_order_through_same_dispatch(monkeypatch) -> None:
    sent: list[list[str]] = []

    def run_editor(argv: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        Path(argv[-1]).write_text("replacement text", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("EDITOR", "fake-editor")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(bridge_tui.subprocess, "run", run_editor)
    monkeypatch.setattr(bridge_tui.curses, "def_prog_mode", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "endwin", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "reset_prog_mode", lambda: None)
    state = bridge_tui._ConsoleState(
        "M8",
        lambda _mission, command: ('{"id":42,"prompt":"old"}' if shlex.split(command)[:2] == ["task", "show"] else sent.append(shlex.split(command)) or "amended"),
        lambda _id: {},
        full=True,
        snapshot={"tasks": [{"id": 42, "title": "old", "status": "READY"}]},
    )
    bridge_tui._edit_task_prompt(FakeWindow(24, 80), state, bridge_tui._selected_task_id(state))  # type: ignore[arg-type]
    _wait_for(lambda: not state.command_pending, state)
    assert sent and sent[0][0:2] == ["amend", "42"]
    assert not Path(sent[0][-1]).exists()


def test_log_page_keys_request_older_and_newer_snapshots_without_changing_orders():
    state = bridge_tui._ConsoleState('mission',lambda *_:'',lambda _id:{},full=True)
    state.choose_view(4)
    state.handle_key('KEY_PPAGE')
    assert state.event_offset == 20
    assert state.task_offset == 0
    state.handle_key('KEY_NPAGE')
    state.handle_key('KEY_NPAGE')
    assert state.event_offset == 0


def test_failed_task_inspection_preserves_failure_details() -> None:
    task = {"id": 7, "status": "FAILED", "prompt": "inspect trace", "title": "failure evidence"}
    import json
    response = bridge_tui._command_feedback("task show --task 7", json.dumps(task))
    assert json.loads(response) == task
    assert bridge_tui._command_feedback("cancel 7", '{"status":"FAILED","result":{"error":"blocked"}}') == "Rejected: blocked"


def test_parsed_control_transition_opens_requested_view_after_wrapper_returns(monkeypatch) -> None:
    calls = []
    actions = iter([bridge_tui.ControlViewAction("other", "Usage"), 0])
    monkeypatch.setattr(bridge_tui.curses, "wrapper", lambda callback: next(actions))
    monkeypatch.setattr(bridge_tui, "run_control", lambda mid, dispatch, snapshot, **kwargs: calls.append((mid, kwargs)))
    assert bridge_tui.run_console("source", lambda *_: "", lambda *_: {}) == 0
    assert calls == [("other", {"initial_view": "Usage"})]


def test_explicit_current_mission_view_command_expands_without_terminal_transition() -> None:
    state = bridge_tui._ConsoleState("current", lambda *_: "", lambda *_: {}, full=False)
    state._command_result = bridge_tui.ControlViewAction("current", "Usage")
    state.command_pending = True
    state.refresh()
    assert state.view == "Usage" and state.expand_requested
    assert not state.command_pending and state.terminal_action is None
