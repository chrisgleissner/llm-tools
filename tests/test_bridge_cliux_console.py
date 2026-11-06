"""CLIUX console and station findings: command line, keys, strip, Actions view, wording, times and footers.

Each test names the finding it covers. They run in process against recording fakes; none starts tmux.
"""

from __future__ import annotations

import curses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge, bridge_actions, bridge_format, bridge_help, bridge_launcher, bridge_monitor, bridge_runtime, bridge_tui, bridge_wording, terminal
from llm_tools.bridge_input import cell_width
from llm_tools.bridge_store import BridgeStore
from tests.test_bridge_additional_acceptance import Window
from tests.test_bridge_review_hold import _hooks, _mission
from tests.test_bridge_tui_experience import Screen, full_snapshot, make_state

NOW = 2_000_000_000


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    monkeypatch.setattr(bridge_tui.curses, "curs_set", lambda _value: None)
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", str(NOW))
    monkeypatch.delenv("LLM_BRIDGE_SURFACE", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)


def _settle(state) -> None:
    deadline = time.monotonic() + 3
    while state.command_pending and time.monotonic() < deadline:
        state.refresh(force=True)
        time.sleep(0.01)
    state.refresh(force=True)
    assert not state.command_pending


# --- CLIUX-011: command line -----------------------------------------------------------------


def test_reject_reason_starting_with_a_slash_is_refused() -> None:
    waiting = lambda: [1]  # noqa: E731
    for words in (["reject", "1", "/cancel"], ["reject", "/cancel"], ["reject", "1/cancel"]):
        expanded, problem = bridge.expand_reject_alias(words, waiting)
        assert expanded is None and problem is not None and "cannot start with '/'" in problem, words
    expanded, problem = bridge.expand_reject_alias(["reject", "1", "2/3", "tests", "failed"], waiting)
    assert problem is None and expanded is not None and expanded[-1] == "--reason=2/3 tests failed"


def test_up_and_down_recall_history_while_the_command_line_has_focus() -> None:
    state = make_state()
    state.history.extend(["/status", "/start"])
    state.input_focused = True
    state.handle_key("KEY_UP")
    assert state.command == "/start"
    state.handle_key("KEY_UP")
    assert state.command == "/status"
    state.handle_key("KEY_DOWN")
    assert state.command == "/start"
    state.handle_key("KEY_DOWN")
    assert state.command == ""


def test_the_recall_hint_names_a_key_that_recalls_the_failed_command() -> None:
    state = make_state()
    state.dispatch = lambda _mission, _command: "error: nope"
    for char in "/start":
        state.handle_key(char)
    state.handle_key("\n")
    _settle(state)
    hint = state.output[-1].rsplit(bridge_tui._SEP, 1)[-1]
    assert hint == "Ctrl-P recalls the command" and "↑" not in hint
    assert state.command == "" and not state.input_focused  # Up would move the body selection here
    state.handle_key("\x10")
    assert state.command == "/start" and state.input_focused


# --- CLIUX-014: keys -------------------------------------------------------------------------


def test_help_keys_lists_the_remaining_key_exceptions_in_one_table() -> None:
    text = bridge_help.topic_text("keys")
    assert "Keys that mean different things in different places" in text
    for row in ("Ctrl-D", "Ctrl-E", "Ctrl-K", "Ctrl-R", "Ctrl-X", "Up/Down", "o v l i"):
        assert row in text, row
    assert "F-keys only switch views" in text
    assert "F2/Ctrl-S" not in text and "Ctrl-D discards" not in text


def test_console_f_keys_only_switch_views_and_ctrl_d_exits() -> None:
    state = make_state()
    state.handle_key("KEY_F2")
    assert state.view == "Tasks"
    assert state.handle_key("\x04") is False  # Ctrl-D exits the console, as in every other place


# --- CLIUX-022: station strip and review message ----------------------------------------------


def _exited_snapshot(*, executing: bool) -> dict[str, object]:
    snapshot = full_snapshot()
    snapshot["current_task"] = {"id": 2, "ordinal": 2, "title": "second task", "status": "RUNNING"}
    snapshot["station_context"] = {"executing_operation": "op-1" if executing else None, "operation_status": "RUNNING" if executing else "EXITED"}
    snapshot["operations"] = [{"id": "op-1", "task_id": 2, "status": "RUNNING" if executing else "EXITED", "exit_code": None if executing else 0,
                               "created_at": NOW - 90, "updated_at": NOW - 60}]
    return snapshot


def test_the_station_strip_follows_the_provider_exit_instead_of_lagging() -> None:
    running = bridge_tui._bounded_snapshot(_exited_snapshot(executing=True))
    assert "RUNNING" in bridge_tui._station_status_parts(running)[1]
    exited = bridge_tui._bounded_snapshot(_exited_snapshot(executing=False))
    text = bridge_tui._station_status_parts(exited)[1]
    assert "REVIEW" in text and "RUNNING" not in text  # the display word for a task the controller moves to VERIFYING
    assert any("REVIEW" in segment for segment, _role in bridge_tui._station_status_segments(exited))
    failed = _exited_snapshot(executing=False)
    failed["operations"][0]["exit_code"] = 2  # type: ignore[index]
    assert "RUNNING" in bridge_tui._station_status_parts(bridge_tui._bounded_snapshot(failed))[1]  # only a clean exit moves on


def test_the_review_message_fits_eighty_columns_next_to_the_exit_status(tmp_path: Path, monkeypatch) -> None:
    hints: list[str] = []

    class HintBackend:
        def set_review_hint(self, handle: object, text: str) -> bool:
            hints.append(text)
            return True

    monkeypatch.setattr(bridge_runtime, "TmuxTerminalBackend", HintBackend)
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission_id, (first, _second) = _mission(store, tmp_path)
        hooks = _hooks(store, mission_id, tmp_path, handle=object())
        hooks._publish_review_hint(store.get_mission(mission_id), first)
    footer = f"Provider exited (status 0) {bridge_tui._SEP}{hints[0]}"
    assert hints[0] == "Task 1 ready for review · Ctrl-] B then /approve"
    assert cell_width(footer) <= 80 and "/reject" not in hints[0]


# --- CLIUX-023: outside station, one-line exit -------------------------------------------------


def test_controls_opened_in_the_users_terminal_say_they_are_outside_the_station() -> None:
    state = make_state(full_snapshot())
    assert "outside station" not in bridge_tui._header(state, 100)
    state.outside_station = True
    assert bridge_tui._header(state, 100).endswith(f"{bridge_tui._SEP}outside station{bridge_tui._SEP}/attach")
    state.snapshot["mission"]["status"] = "COMPLETED"  # type: ignore[index]
    header = bridge_tui._header(state, 100)
    assert header.endswith("outside station") and "/attach" not in header
    assert cell_width(bridge_tui._header(state, 40)) <= 40


def test_the_saved_notice_is_withdrawn_before_controls_reopen_and_a_finished_mission_exits_to_the_shell(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    def run(status: str) -> tuple[list[str], str | None]:
        window = Window(["n", *"Objective", *(["\t"] * 5), "\n"])
        monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
        monkeypatch.setattr(bridge_launcher, "_draw", lambda *_: None)
        monkeypatch.setattr(bridge_launcher, "_mission_status", lambda _id: status)
        seen: list[str] = []
        at_control: list[str | None] = []

        def dispatch(args: list[str]) -> int:
            seen.append(args[0])
            if args[0] == "mission":
                print(json.dumps({"mission": {"id": "m1"}}))
            if args[0] == "control":
                at_control.append(bridge_tui.JUST_SAVED_MISSION)
            return 0

        assert bridge_launcher.run_launcher(dispatch) == 0
        return seen, at_control[0] if at_control else "not opened"

    seen, saved = run("RUNNING")
    assert seen == ["mission", "start", "control"] and saved is None  # no stale "saved as ID" after the provider ran
    seen, _saved = run("COMPLETED")
    assert seen == ["mission", "start"]  # nothing to control on a finished mission


def test_the_detach_notice_for_a_finished_mission_is_one_line_with_the_task_count(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    mission_id = "f292f491" + "0" * 24
    with BridgeStore(tmp_path / "state" / "bridge.sqlite3") as store:
        store.create_mission("c64bridge", "objective", cwd=tmp_path, mission_id=mission_id, initial_prompt="task")
        store.set_mission_status(mission_id, "CANCELLED")
    notice = terminal.detach_notice(f"mission-{mission_id}")
    assert notice == "Mission f292f491 cancelled \u00b7 0/1 tasks"  # no "Detached from", no name, no wrapped " \u00b7 " tail


def test_fit_line_cuts_to_the_terminal_width_with_an_ellipsis() -> None:
    assert terminal.fit_line("short", 80) == "short"
    cut = terminal.fit_line("Mission f292f491 completed · 1/1 tasks", 20)
    assert cell_width(cut) <= 20 and cut.endswith("…") and not cut.startswith(" ")


# --- CLIUX-029 / CLIUX-059: Actions view -------------------------------------------------------


def _labels(status: str, **kwargs: Any) -> list[str]:
    return [action.label for action in bridge_actions.actions_for(status, **kwargs)]


def test_start_and_resume_are_one_context_action() -> None:
    assert _labels("CREATED")[0] == "Start" and "Resume" not in _labels("CREATED")
    stopped = bridge_actions.actions_for("STOPPED")
    assert [a.label for a in stopped].count("Start") + [a.label for a in stopped].count("Resume") == 1
    assert (stopped[0].label, stopped[0].command) == ("Resume", "resume")
    paused = bridge_actions.actions_for("PAUSED")
    assert (paused[0].label, paused[0].command) == ("Resume", "resume")
    assert bridge_actions.actions_for("CREATED")[0].command == "start"


def test_actions_that_need_a_provider_terminal_are_hidden_for_a_headless_mission() -> None:
    assert "Attach" in _labels("RUNNING") and "Leave" in _labels("RUNNING")
    headless = _labels("RUNNING", has_terminal=False)
    assert "Attach" not in headless and "Leave" not in headless and "Pause" in headless
    snapshot = full_snapshot()
    snapshot["station_mode"] = {"configured_mode": "headless", "actual_mode": "headless", "source": "x"}
    assert not {"Attach", "Leave"} & {action.label for action in make_state(snapshot, view="Actions").palette_actions()}
    snapshot["station_mode"] = {"configured_mode": "native", "actual_mode": "native", "source": "x"}
    assert {"Attach", "Leave"} <= {action.label for action in make_state(snapshot, view="Actions").palette_actions()}


def test_a_cut_action_description_is_shown_in_full_below_the_list() -> None:
    state = make_state(full_snapshot(), view="Actions")
    names = [action.label for action in state.palette_actions()]
    state.selected = names.index("Suspend until credits recover")
    effect = next(action.effect for action in state.palette_actions() if action.label == "Suspend until credits recover")
    narrow = Screen(24, 80)
    bridge_tui._draw_full(narrow, state)  # type: ignore[arg-type]
    row = next(line for line in narrow.lines if "Suspend until credits recover" in line)
    assert effect not in row  # the description column is cut at 80 columns
    assert any(line.strip() == effect for line in narrow.lines)  # and the full text is on a line of its own
    wide = Screen(30, 140)
    bridge_tui._draw_full(wide, state)  # type: ignore[arg-type]
    assert not any(line.strip() == effect for line in wide.lines)  # nothing extra when the row fits


def test_console_leave_and_cli_detach_have_different_names_and_one_description_each(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission_id = store.create_mission("m", "objective", cwd=tmp_path)["id"]
    leave = bridge.dispatch_console_command(mission_id, "/leave")
    assert leave == terminal.TerminalAction(mission_id, None, "detach")
    assert bridge.dispatch_console_command(mission_id, "detach") == leave  # the old console name keeps working
    action = next(item for item in bridge_actions.ACTIONS if item.command == "leave")
    assert action.typed == "/leave" and "Leave the provider terminal" in action.effect
    assert "/leave" in [name for name, _text in bridge_tui._slash_table()]
    detach = " ".join(bridge_help.topic_text("detach").split())
    assert "another terminal" in detach and "/leave" in detach
    assert "/leave" in bridge_help.topic_text("console") and "/detach" not in bridge_help.topic_text("console")


# --- CLIUX-046 / 060 / 061 / 034: one wording, two surfaces -------------------------------------


def test_next_steps_use_slash_forms_in_the_console_and_shell_forms_in_the_cli() -> None:
    assert bridge_wording.controller_hint("CREATED", "cli", "ab12cd34") == "not started — run: llm-bridge start --mission ab12cd34"
    assert bridge_wording.controller_hint("CREATED", "console") == "not started — type /start"
    assert bridge_wording.controller_hint("STOPPED", "console") == "stopped — type /start or /resume"
    assert bridge_wording.controller_hint("RUNNING", "console") is None
    assert bridge_wording.proposals_row("console", 2) == "2 agent task proposals to review · type /proposals"
    assert "llm-bridge proposal list --mission ab12cd34" in bridge_wording.proposals_row("cli", 1, "ab12cd34")
    assert bridge_wording.approval_row("console", [3]) == "task 3 awaits your review · type /approve 3 (or F7 Actions)"
    cli = bridge_wording.approval_row("cli", [3], "ab12cd34")
    assert cli.startswith("task 3 awaits your review · llm-bridge task approve --mission ab12cd34 --task 3 (or reject: ")
    assert bridge_wording.NEXT_NOTHING == "nothing ready"


def _cli_snapshot(status: str, *, tasks: list[dict[str, Any]], current: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"mission": {"id": "ab12cd34" + "0" * 24, "name": "m", "status": status, "cwd": "/x", "config": {}}, "tasks": tasks,
            "task_counts": {}, "current_task": current, "lifecycle": {"controller_running": False}}


def test_status_words_the_next_step_for_the_surface_it_runs_on(monkeypatch) -> None:
    snapshot = _cli_snapshot("CREATED", tasks=[])
    assert "Controller  not started — run: llm-bridge start --mission ab12cd34" in bridge_format.format_status(snapshot)
    monkeypatch.setenv("LLM_BRIDGE_SURFACE", "console")
    assert "Controller  not started — type /start" in bridge_format.format_status(snapshot)
    monkeypatch.delenv("LLM_BRIDGE_SURFACE")
    assert "Next        nothing ready" in bridge_format.format_status(snapshot)  # the console says the same


def test_the_console_runs_commands_with_the_console_surface(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission_id = store.create_mission("m", "objective", cwd=tmp_path)["id"]
    text = str(bridge.dispatch_console_command(mission_id, "/status"))
    assert "not started — type /start" in text and "llm-bridge start" not in text


def test_the_response_pager_uses_the_dot_separator_of_every_other_surface() -> None:
    state = make_state()
    state.output.extend(["line one", "line two"])
    screen = Screen(10, 100)
    bridge_tui._draw_response_overlay(screen, state)  # type: ignore[arg-type]
    assert screen.lines[1] == "Arrows/PgUp/PgDn scroll · Esc/Enter close · F1-9/Tab switch view"


def test_a_capacity_wait_in_the_console_names_its_options_not_the_screen_it_is_on() -> None:
    waiting = {"reason": "capacity", "entries": [{"name": "copilot", "window": "monthly", "reset_epoch": NOW + 24 * 86400 + 5 * 3600}]}
    pane = bridge_monitor.waiting_message(waiting, NOW)
    assert pane.endswith("Ctrl-] then B for options.")  # the provider pane points to the controls
    console = bridge_monitor.waiting_message(waiting, NOW, console=True)
    assert "Ctrl-]" not in console and console.endswith("/rotation to add a provider · /stop to stop waiting")
    snapshot = full_snapshot()
    snapshot["waiting"] = waiting
    assert bridge_tui._needs_you_rows(bridge_tui._bounded_snapshot(snapshot))[0] == console


def _stopped_snapshot() -> dict[str, object]:
    task = {"id": 1, "ordinal": 1, "title": "Write hello.txt", "status": "READY"}
    return {"mission": {"id": "M1", "name": "Write hello.txt", "status": "STOPPED"}, "tasks": [task], "current_task": task,
            "task_counts": {"READY": 1}, "lifecycle": {"status": "STOPPED"}}


def test_a_stopped_mission_with_a_ready_task_agrees_with_itself() -> None:
    state = make_state(_stopped_snapshot())
    rows = state.rows()
    now_row = next(row for row in rows if row.startswith("Now"))
    next_row = next(row for row in rows if row.startswith("Next"))
    assert now_row.strip().endswith("idle") and "task 1" in next_row and "nothing ready" not in next_row
    assert next(row for row in rows if row.startswith("Tasks")).endswith("1 ready")
    text, _role = bridge_tui._status_strip(bridge_tui._bounded_snapshot(_stopped_snapshot()))
    assert text == "stopped · task 1 ready · type /start" and "no task waiting" not in text


def test_the_cli_status_of_a_stopped_mission_lists_the_ready_task_as_next() -> None:
    task = {"id": 1, "ordinal": 1, "title": "Write hello.txt", "status": "READY", "origin": "operator"}
    text = bridge_format.format_status(_cli_snapshot("STOPPED", tasks=[task], current=task))
    assert "Current     none" in text and "Next        task 1" in text


def test_a_finished_mission_names_what_to_do_next() -> None:
    snapshot = {"mission": {"id": "M1", "name": "m", "status": "COMPLETED"}, "tasks": [{"id": 1, "ordinal": 1, "title": "t", "status": "COMPLETED"}]}
    row = next(row for row in make_state(snapshot).rows() if row.startswith("Next"))
    assert "Done · /missions switches to another mission · Ctrl-D leaves the controls" in row
    cli = bridge_format.format_status(_cli_snapshot("COMPLETED", tasks=[]))
    assert "Next        Done · llm-bridge missions lists your missions · llm-bridge mission create starts a new one" in cli
    assert bridge_wording.finished_next("CANCELLED", "console").startswith("Cancelled ·")
    assert bridge_wording.finished_next("RUNNING", "console") is None


# --- CLIUX-047: Health agrees with Sessions ---------------------------------------------------


def test_health_reports_a_finished_operation_as_finished() -> None:
    snapshot = _exited_snapshot(executing=False)
    snapshot["tasks"] = [{"id": 2, "ordinal": 2, "title": "second task", "status": "VERIFYING"}]
    snapshot["current_task"] = {"id": 2, "ordinal": 2, "title": "second task", "status": "VERIFYING"}
    snapshot["execution"] = {"iteration": 1, "operation_elapsed_seconds": 30, "seconds_since_verified_progress": None}
    row = next(row for row in make_state(snapshot, view="Health").rows() if row.startswith("Execution"))
    assert "last operation exited 0 · 1m ago · awaiting review" in row and "operation running" not in row
    snapshot["station_context"] = {"executing_operation": "op-2", "operation_status": "RUNNING"}
    snapshot["operations"] = [{"id": "op-2", "task_id": 2, "status": "RUNNING", "exit_code": None, "created_at": NOW - 90, "updated_at": NOW - 60}]
    running = next(row for row in make_state(snapshot, view="Health").rows() if row.startswith("Execution"))
    assert "operation running" in running


# --- CLIUX-073: dates --------------------------------------------------------------------------


def test_times_that_are_not_from_today_carry_a_date(monkeypatch) -> None:
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    noon = time.mktime((2026, 11, 6, 12, 0, 0, 0, 0, 0))
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", str(int(noon)))
    today = int(noon) - 3600
    assert bridge_tui._local_clock(today) == "11:00:00"
    recent = bridge_tui._local_clock(int(noon) - 3 * 86400)
    assert recent == time.strftime("%a 12:00", time.gmtime(noon - 3 * 86400)) and "2026" not in recent
    assert bridge_tui._local_clock(int(noon) - 30 * 86400) == "2026-10-07 12:00"
    assert bridge_tui._local_clock("not a time") == "--:--:--"
    for value in (today, int(noon) - 3 * 86400, int(noon) - 30 * 86400):
        row = f"{bridge_tui._local_clock(value)}  Waiting for capacity"
        assert bridge_tui._activity_row_segments(row, "info")[0][0] == row.split("  ")[0]  # the clock keeps its own colour
    assert bridge_format._event_time({"created_at": "2026-11-06T18:51:56+00:00"}).startswith("2026-11-06 ")


# --- CLIUX-076: footers --------------------------------------------------------------------------


def test_console_footer_keeps_the_quit_key_and_help_and_does_not_repeat_the_prompt() -> None:
    state = make_state(full_snapshot())
    for width in (140, 100, 80, 60, 45, 30):
        hint = bridge_tui._hint(state, width)
        assert cell_width(hint) <= width and hint.endswith("? help") and "Ctrl-D exit" in hint, (width, hint)
    assert "Ctrl-K" not in bridge_tui._hint(state, 200)  # the prompt line says it
    assert bridge_tui._placeholder(state).endswith("Ctrl-K actions")
    for view in bridge_tui.VIEWS:
        assert "Ctrl-D exit" in bridge_tui._hint(make_state(view=view), 45), view


def test_home_list_footer_drops_low_priority_hints_and_keeps_help_and_quit() -> None:
    launcher = bridge_launcher.Launcher("/tmp", missions=[{"id": "0123456789abcdef", "name": "m", "status": "CREATED", "config": {}, "cwd": "/tmp",
                                                            "created_at": "2026-11-06T00:00:00+00:00", "updated_at": "2026-11-06T00:00:00+00:00"}] * 8, selected=1)
    for width in (120, 80, 60, 40, 30):
        footer = bridge_launcher.list_footer(launcher, width)
        assert cell_width(footer) <= width and footer.endswith("Q quit") and "? help" in footer, (width, footer)
    assert "Enter open" in bridge_launcher.list_footer(launcher) and "Enter controls" not in bridge_launcher.list_footer(launcher)
    assert "/ filter" in bridge_launcher.list_footer(launcher) and "/ filter" not in bridge_launcher.list_footer(launcher, 60)
