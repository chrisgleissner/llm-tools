"""Bounded projection contracts for every optional TUI data shape."""

from __future__ import annotations

from llm_tools import bridge_tui


def test_text_command_feedback_and_selection_fallbacks() -> None:
    assert bridge_tui._text("a\nb\x00c", 20) == "a b c"
    assert bridge_tui._text("abcdef", 3) == "abc"
    assert bridge_tui._items({"not": "a list"}) == []
    assert bridge_tui._items([{}, "drop", None]) == [{}]
    assert bridge_tui._dict([]) == {}
    assert bridge_tui._command_feedback("verify 1", "not json") == "not json"
    assert bridge_tui._command_feedback("status", "[]") == "[]"
    assert bridge_tui._command_feedback("status", '{"status":"APPLIED","result":{"kind":"pause","detail":"after iteration"}}') == "Applied: pause steering — after iteration"
    assert bridge_tui._command_feedback("verify 1", '{"status":"FAILED","result":{}}') == "Rejected: failed"
    assert bridge_tui._mission({"id": "M"}) == {"id": "M"}


def test_legacy_station_map_projection_is_deduplicated_bounded_and_truthful() -> None:
    rows, context = bridge_tui._station_projection({
        "configured": {
            "providers": ["codex", "claude", "codex"],
            "routes": ["route-a"],
            "models": {"codex": "configured-model"},
            "efforts": {"codex": "high"},
            "mode": "native",
        },
        "observed": {"station": "route-a", "model": "observed-model", "status": "attached", "operation_status": "RUNNING"},
        "viewed_station": "claude",
        "last_launched_station": "codex",
        "executing_operation": "op-3",
        "provider_liveness": "unknown",
    })
    assert [row["name"] for row in rows] == ["codex", "claude", "route-a"]
    assert rows[0]["model"] == "configured-model" and rows[0]["observed_model"] == "unknown"
    assert rows[2]["observed_model"] == "observed-model" and rows[2]["status"] == "attached"
    assert context["viewed_station"] == "claude" and context["executing_operation"] == "op-3"
    assert context["provider_liveness"] == "unknown"
    assert bridge_tui._station_projection({}) == ([], {})
    appended_rows, appended_context = bridge_tui._station_projection({
        "configured": {"providers": ["codex"], "routes": [], "mode": "headless"},
        "observed": {"station": "unconfigured-route", "model": "observed"},
    })
    assert [row["name"] for row in appended_rows] == ["codex", "unconfigured-route"]
    assert appended_rows[1]["observed_model"] == "observed" and appended_context["mode"] == "headless"
    list_rows, list_context = bridge_tui._station_projection([{"id": "kilo", "model": "configured"}])
    assert list_rows == [{"id": "kilo", "model": "configured"}] and list_context == {}

    bounded_dict = bridge_tui._bounded_snapshot({
        "stations": {
            "configured": {"providers": ["codex"], "routes": [], "models": {"codex": "gpt"}, "mode": "native"},
            "observed": {"station": "codex", "model": "gpt-live", "status": "verified"},
            "viewed_station": "codex", "executing_operation": "op-1",
        },
    })
    assert bounded_dict["station_context"]["viewed_station"] == "codex"
    assert bounded_dict["stations"][0]["observed_model"] == "gpt-live"


def test_bounded_task_and_capacity_projection_keeps_supported_scalars_only() -> None:
    nested = bridge_tui._bounded_task_value({
        "prompt": "line one\nline\x00two",
        "title": "title",
        "priority": 4,
        "unbounded_payload": {"secret": "discard"},
        "changes": [{"prompt": "revised\ntext", "unknown": "discard"}],
    })
    assert nested == {
        "prompt": "line one\nline two", "title": "title", "priority": 4,
        "changes": [{"prompt": "revised\ntext"}],
    }
    assert str(bridge_tui._bounded_task_value(object())).startswith("<object object at 0x")
    projected = bridge_tui._project_capacity({
        "source": "event", "non_scalar": {"secret": True},
        "selection": {
            "route": "codex", "decision": {"usable": True, "private": []},
            "decisions": [{"provider": "codex", "windows": [{"name": "5h", "remaining_percent": 50, "private": []}]}],
        },
        "wait": {"reason": "rate-limited", "metadata": []},
    })
    assert projected["selection"]["decision"] == {"usable": "True"}
    assert projected["selection"]["decisions"][0]["windows"] == [{"name": "5h", "remaining_percent": "50"}]
    assert projected["wait"] == {"reason": "rate-limited"}
    assert bridge_tui._project_capacity({"source": "event", "selection": {}, "wait": {}}) == {"source": "event"}


def test_capacity_summary_classifies_active_alternate_and_opaque_routes() -> None:
    summaries = bridge_tui._capacity_summaries({
        "capacity": {"selection": {
            "route": "codex",
            "decisions": [
                {"route": "alt", "usable": True, "selected_model": "model-a", "windows": []},
                {"route": "codex", "windows": [
                    {"name": "5h", "kind": "reset_window", "estimated_remaining": "2h", "remaining_percent": 61, "reset_epoch": 1799999999},
                    {"name": "subscription", "kind": "opaque"},
                ]},
                {"provider": "other", "usable": False, "windows": [{"kind": "budget"}]},
            ],
        }},
    })
    assert summaries[0].startswith("ACTIVE codex")
    assert "5h est 2h (61%; reset " in summaries[0]
    assert "subscription: quota unmeasured" in summaries[0]
    assert any(row.startswith("READY alt / model-a | capacity unavailable") for row in summaries)
    assert any(row.startswith("ALT other") and "unknown" in row for row in summaries)
    assert bridge_tui._capacity_summaries({}) == []


def test_navigation_hit_testing_clamps_inputs_and_uses_current_view_markers() -> None:
    labels = bridge_tui._nav_labels(0)
    assert labels[0].startswith("[1 Bridge]") and labels[-1] == "8 Usage"
    assert bridge_tui._nav_index_at(0, 0, 80, " ") == 0
    assert bridge_tui._nav_index_at(79, 0, 80, " ") == 7
    assert bridge_tui._nav_index_at(100, 0, 80, " ") == 7
    assert bridge_tui._nav_line(1, "  ").count("  ") > 0


def test_view_rows_cover_empty_and_legacy_fallbacks() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda _id: {}, full=True)
    state.choose_view(1)
    state.task_detail = True
    assert state.rows() == ["No task selected."]
    state.task_detail = False
    assert state.rows() == ["No tasks recorded."]

    state.choose_view(2)
    assert state.rows() == ["No stations recorded."]
    state.snapshot = {"operations": [{"station": "codex", "model": "gpt", "status": "EXITED"}]}
    assert state.rows() == ["codex  gpt  EXITED"]

    state.choose_view(3)
    assert state.rows() == ["System and capacity status is not available in this snapshot."]
    state.snapshot = {"routes": "codex,claude", "health": "warning", "capacity": {"source": "event"}}
    rows = state.rows()
    assert "routes: codex,claude" in rows and "health: warning" in rows and "capacity: source=event" in rows

    state.choose_view(4)
    assert state.rows() == ["No recent mission events."]
    state.snapshot = {"events": [{"id": 1, "created_at": "then"}]}
    assert state.rows() == ["1  event  then"]

    state.choose_view(5)
    assert state.rows() == ["No session activity recorded."]
    state.snapshot = {"stations": [{"id": "codex", "model": "configured", "observed_model": "unknown"}]}
    assert "configured model configured" in state.rows()[0]

    state.choose_view(6)
    assert state.rows()[0].startswith("> help")
    assert len(state.rows()) == len(bridge_tui.PALETTE_COMMANDS)
    state.choose_view(99)
    assert state.view == "Palette"


def test_empty_and_command_key_navigation_paths_remain_predictable() -> None:
    calls: list[str] = []
    state = bridge_tui._ConsoleState("M", lambda _mission, line: calls.append(line) or "ok", lambda _id: {}, full=True)
    assert not state.handle_key("\x04")
    state.handle_key("\t")
    assert state.view == "Orders"
    state.handle_key("KEY_BTAB")
    assert state.view == "Bridge"
    state.choose_view(2)
    state.handle_key("KEY_ENTER")
    assert not state.command_pending  # no station selection to dispatch
    state.handle_key("\x10")
    state.handle_key("\x0e")
    state.handle_key("KEY_Finvalid")
    assert state.view == "Stations"
    for char in "x":
        state.handle_key(char)
    state.handle_key("KEY_UP")  # arrows do not navigate while a command is being entered
    assert state.command == "x" and state.selected == 0
    state.handle_key("KEY_BACKSPACE")
    assert state.command == ""
    state.search_editing = True
    state.command = "filter"
    state.handle_key("\x10")
    state.handle_key("\x03")
    assert state.command == "" and not state.search_editing
    state.handle_key("KEY_RESIZE")
    state.handle_key("KEY_MOUSE")
    state.handle_mouse(0, 0, 0, 80)
    state.handle_mouse(0, 0, 24, 0)
    assert calls == []


def test_optional_snapshot_sections_and_ui_helpers_cover_empty_and_present_values() -> None:
    sparse = bridge_tui._bounded_snapshot({
        "controller": {"status": "ok"}, "routes": ["route"], "execution": None,
        "station_mode": {}, "lifecycle": {"heartbeat": {}}, "capacity": {},
        "health": {}, "usage": {"status": "ready", "text": "dashboard"},
        "pending_commands": "not a list", "task_total": "not numeric",
    })
    assert sparse["controller"] == "{'status': 'ok'}"
    assert sparse["routes"] == "['route']" and sparse["execution"] == "None"
    assert sparse["pending_command_count"] == 0 and "task_total" not in sparse
    assert sparse["usage"] == {"status": "ready", "text": "dashboard"}
    assert "station_mode" not in sparse and "capacity" not in sparse
    assert sparse["lifecycle"] == {}

    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda _id: {}, full=True, snapshot={"task_total": 0, "tasks": []})
    assert bridge_tui._orders_summary(state) == "Orders: 0 total; showing 0-0"
    state.search_query = "foo"
    assert bridge_tui._orders_summary(state) == "Orders: 0 total matching /foo/; showing 0-0"
    assert bridge_tui._command_display("ignored", 0) == ""
    assert bridge_tui._mission({"mission": {"id": "nested"}}) == {"id": "nested"}


def test_key_state_transitions_cover_pending_search_and_navigation_modes() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda _id: {}, full=False)
    assert state._dispatch("   ")
    assert not state.history
    state.search_editing = True
    state.command = "find"
    state.handle_key("\x10")
    state.handle_key("\x0e")
    assert state.command == "find"
    state.handle_key("\x1b")
    assert not state.search_editing and state.command == ""

    state.choose_view(3)
    state.handle_key("KEY_NPAGE")
    assert state.selected == 5
    state.handle_key("KEY_PPAGE")
    assert state.selected == 0
    state.command = "typed"
    state.handle_key("KEY_RIGHT")
    assert state.view == "Systems"
    state.handle_key("\x04")
    assert state.command == ""  # First Ctrl-D clears an input line.
    state.command_pending = True
    assert state.handle_key("\x04")
    assert "Wait for the command result" in state.output[-1]
    state.command_pending = False
    assert not state.handle_key("\x04")

    state.command = "z" * (bridge_tui._MAX_COMMAND_CHARS - 1)
    state.handle_key("x")
    assert len(state.command) == bridge_tui._MAX_COMMAND_CHARS
    state.handle_key("y")
    assert len(state.command) == bridge_tui._MAX_COMMAND_CHARS
