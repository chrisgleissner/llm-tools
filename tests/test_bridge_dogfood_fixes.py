"""Dogfooding defects: nested attach, start flow, capacity text, titles and controller liveness."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_format as fmt, bridge_tui, terminal
from llm_tools.bridge_store import BridgeStore, MissionLock, derive_title
from llm_tools.terminal import TerminalAction
from tests.conftest import scaled


# --- 1. attach from controls inside a foreign tmux --------------------------------------


class _Backend:
    def __init__(self, bridge_sockets: set[Path] | None = None, owned_error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.bridge_sockets = bridge_sockets or set()
        self.owned_error = owned_error
        self.tmux_seen: list[str | None] = []

    def server_owned(self, socket: Path) -> bool:
        if self.owned_error is not None:
            raise self.owned_error
        return Path(socket) in self.bridge_sockets

    def focus(self, handle, *, pane, expand=False):
        self.calls.append(("focus", handle, pane, expand))

    def attach(self, handle):
        self.tmux_seen.append(os.environ.get("TMUX"))
        self.tmux_seen.append(os.environ.get("TMUX_PANE"))
        self.calls.append(("attach", handle))

    def detach_client(self, handle, *, client_name=None):
        self.calls.append(("detach", handle, client_name))

    def switch_client_to(self, source, destination, *, client_name=None):
        self.calls.append(("switch", source, destination, client_name))


@pytest.fixture()
def handles(tmp_path: Path, monkeypatch):
    source = terminal.TerminalHandle(tmp_path / "source.sock", tmp_path / "source.sock.conf", "source", "%0", "%1", 6)
    other = terminal.TerminalHandle(tmp_path / "other.sock", tmp_path / "other.sock.conf", "other", "%2", "%3", 6)
    monkeypatch.setattr("llm_tools.bridge._load_handle", lambda mission: source if mission == "a" else other)
    return source, other


def _install(monkeypatch, backend: _Backend) -> None:
    monkeypatch.setattr(terminal, "TmuxTerminalBackend", lambda: backend)


def test_no_tmux_attaches_directly(monkeypatch, handles) -> None:
    source, other = handles
    backend = _Backend()
    _install(monkeypatch, backend)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    bridge_tui._execute_terminal_action("a", TerminalAction("b"))
    assert backend.calls == [("attach", other)]


def test_foreign_tmux_attaches_without_tmux_variables_and_restores_them(monkeypatch, handles, tmp_path: Path) -> None:
    source, other = handles
    backend = _Backend(bridge_sockets={source.socket_path})
    _install(monkeypatch, backend)
    monkeypatch.setenv("TMUX", f"{tmp_path / 'user-tmux.sock'},99,0")
    monkeypatch.setenv("TMUX_PANE", "%7")
    bridge_tui._execute_terminal_action("a", TerminalAction("a"))
    bridge_tui._execute_terminal_action("a", TerminalAction("b"))
    bridge_tui._execute_terminal_action("a", TerminalAction("b", "/dev/pts/5", "detach"))
    assert backend.calls == [
        ("focus", source, "provider", True),
        ("attach", source),
        ("attach", other),
        ("detach", other, "/dev/pts/5"),
    ]
    assert backend.tmux_seen == [None, None, None, None]
    assert os.environ["TMUX"].endswith(",99,0") and os.environ["TMUX_PANE"] == "%7"


def test_own_station_still_focuses_and_switches(monkeypatch, handles) -> None:
    source, other = handles
    backend = _Backend(bridge_sockets={source.socket_path, other.socket_path})
    _install(monkeypatch, backend)
    monkeypatch.setenv("TMUX", f"{source.socket_path},1,0")
    monkeypatch.setenv("TMUX_PANE", source.bridge_pane)
    bridge_tui._execute_terminal_action("a", TerminalAction("a"))
    bridge_tui._execute_terminal_action("a", TerminalAction("b", "/dev/pts/3"))
    assert backend.calls == [("focus", source, "provider", True), ("switch", source, other, "/dev/pts/3")]
    monkeypatch.setenv("TMUX_PANE", source.provider_pane)
    with pytest.raises(PermissionError, match="owned Bridge pane"):
        bridge_tui._execute_terminal_action("a", TerminalAction("b"))


def test_other_bridge_station_is_refused_and_unqueryable_servers_use_the_socket_name(monkeypatch, handles, tmp_path: Path) -> None:
    source, other = handles
    monkeypatch.setenv("TMUX", f"{other.socket_path},1,0")
    monkeypatch.setenv("TMUX_PANE", other.bridge_pane)
    backend = _Backend(bridge_sockets={source.socket_path, other.socket_path})
    _install(monkeypatch, backend)
    with pytest.raises(PermissionError, match="owned Bridge pane"):
        bridge_tui._execute_terminal_action("a", TerminalAction("b"))
    # Server cannot be queried: a Bridge-style socket name is still refused ...
    station_socket = tmp_path / ("m-" + "0123456789abcdef0123" + ".sock")
    unknown = _Backend(owned_error=RuntimeError("cannot determine tmux server ownership"))
    _install(monkeypatch, unknown)
    monkeypatch.setenv("TMUX", f"{station_socket},1,0")
    with pytest.raises(PermissionError, match="owned Bridge pane"):
        bridge_tui._execute_terminal_action("a", TerminalAction("b"))
    assert backend.calls == [] and unknown.calls == []
    # ... while any other unqueryable server is the user's own tmux.
    monkeypatch.setenv("TMUX", f"{tmp_path / 'default'},1,0")
    bridge_tui._execute_terminal_action("a", TerminalAction("b"))
    assert unknown.calls == [("attach", other)]


# --- 2. start from the controls opens a native provider ----------------------------------


def _start_console(monkeypatch, tmp_path: Path, config: dict, status: str):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission = store.create_mission("Start", "Objective", cwd=tmp_path, config=config)
        store.set_mission_status(mission["id"], "RUNNING")
        if status != "RUNNING":
            store.set_mission_status(mission["id"], status)
    monkeypatch.setattr(bridge.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "ok", ""))
    return mission["id"]


def test_console_start_of_native_mission_returns_attach_action(monkeypatch, tmp_path: Path) -> None:
    mission_id = _start_console(monkeypatch, tmp_path, {"native_tui": True}, "RUNNING")
    assert bridge.dispatch_console_command(mission_id, "start") == TerminalAction(mission_id, None, "attach")
    assert bridge.dispatch_console_command(mission_id, "/start") == TerminalAction(mission_id, None, "attach")


@pytest.mark.parametrize("config,status", [({"headless": True}, "RUNNING"), ({}, "RUNNING"), ({"native_tui": True}, "STOPPED")])
def test_console_start_keeps_text_reply_without_native_station(monkeypatch, tmp_path: Path, config, status) -> None:
    mission_id = _start_console(monkeypatch, tmp_path, config, status)
    result = json.loads(bridge.dispatch_console_command(mission_id, "start"))
    assert result["status"] == "APPLIED"


# --- 3. capacity text --------------------------------------------------------------------


def test_runout_beyond_reset_reads_lasts_past_reset(monkeypatch) -> None:
    monkeypatch.setenv("LLM_USAGE_NOW_EPOCH", "2000000000")
    now = 2_000_000_000
    window = {"name": "5h", "kind": "reset_window", "remaining_percent": 75, "estimated_remaining": "2d 10h 35m", "reset_epoch": now + 4 * 3600}
    assert bridge_tui._window_summary(window, now) == "5h 75% left, lasts past reset, resets 4h"
    window["estimated_remaining"] = "1h 30m"
    assert bridge_tui._window_summary(window, now) == "5h 75% left, empty in ~1h 30m at this pace, resets 4h"
    window["estimated_remaining"] = "2 hours"
    assert "at this pace" in bridge_tui._window_summary(window, now)
    assert bridge_tui._duration_seconds("~2d 10h 35m") == 2 * 86400 + 10 * 3600 + 35 * 60
    assert bridge_tui._duration_seconds("about 2h") is None and bridge_tui._duration_seconds("") is None


def test_wrap_row_breaks_only_between_parts_and_hangs_under_value() -> None:
    text = "Capacity    codex (active) · 5h 75% left, lasts past reset, resets 4h · weekly 21% left, ~1h 52m at this pace, resets 6d 22h"
    lines = bridge_tui._wrap_row(text, 80, 12)
    assert len(lines) > 1 and all(bridge_tui.cell_width(line) <= 80 for line in lines)
    assert lines[0].startswith("Capacity    codex (active)")
    assert all(line.startswith(" " * 12) and not line[12].isspace() for line in lines[1:])
    assert not any(line.rstrip().endswith(("resets 6d", "resets")) for line in lines)
    assert any("resets 6d 22h" in line for line in lines)
    assert "".join(lines).replace(" ", "").replace(",", "").replace("·", "") == text.replace(" ", "").replace(",", "").replace("·", "")
    assert bridge_tui._wrap_row("short", 80, 12) == ["short"]


def test_wrap_row_breaks_clause_by_clause_then_by_words() -> None:
    text = "Capacity    alpha, bravo charlie delta, echo"
    assert bridge_tui._wrap_row(text, 26, 12) == ["Capacity    alpha,", "            bravo charlie", "            delta,", "            echo"]
    long_clause = "Now         " + "x" * 50
    lines = bridge_tui._wrap_row(long_clause, 30, 12)
    assert all(bridge_tui.cell_width(line) <= 30 for line in lines) and len(lines) > 1


# --- 4. Markdown heading markers ----------------------------------------------------------


@pytest.mark.parametrize("prompt,title", [
    ("# Show provider output inactivity", "Show provider output inactivity"),
    ("## Sub heading  \nbody", "Sub heading"),
    ("\n\n###\n#### Deep\nbody", "Deep"),
    ("#hashtag stays", "#hashtag stays"),
    ("**# bold**", "**# bold**"),
    ("   \n  # Padded   ", "Padded"),
    ("", "fallback"),
    ("###\n", "fallback"),
    ("# " + "w" * 150, "w" * 100),
])
def test_derive_title(prompt: str, title: str) -> None:
    assert derive_title(prompt, "fallback") == title


def test_titles_and_names_drop_heading_markers_but_prompt_is_stored_verbatim(tmp_path: Path, monkeypatch) -> None:
    prompt = "# Show provider output inactivity in LLM Bridge\n\nDetails.\n"
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission(derive_title(prompt, "x"), prompt, cwd=tmp_path, initial_prompt=prompt)
        task = store.get_task(int(mission["initial_task_id"]))
        assert mission["name"] == task["title"] == "Show provider output inactivity in LLM Bridge"
        assert task["prompt"] == task["original_prompt"] == prompt
        added = store.get_task(store.add_task(mission["id"], "## Another\nmore"))
        assert added["title"] == "Another" and added["prompt"] == "## Another\nmore"


# --- 5/6. controller liveness and CLI wording ---------------------------------------------


def test_mission_lock_is_held_probe_does_not_create_or_steal(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    assert MissionLock.is_held(locks, "m1") is False and not locks.exists()
    owner = MissionLock(locks, "m1")
    owner.acquire()
    try:
        assert MissionLock.is_held(locks, "m1") is True
        assert MissionLock.is_held(locks, "m1") is True  # probes do not mask each other
    finally:
        owner.release()
    assert MissionLock.is_held(locks, "m1") is False
    owner.acquire()  # the probe left the lock free
    owner.release()


def test_status_and_list_show_missing_controller(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission = store.create_mission("Stale", "Objective", cwd=tmp_path)
        mid = mission["id"]
    assert bridge.main(["status", "--mission", mid]) == 0
    assert f"Controller  not started — run: llm-bridge start --mission {mid[:8]}" in capsys.readouterr().out
    with bridge.open_store() as store:
        store.set_mission_status(mid, "RUNNING")
    assert bridge.main(["status", "--mission", mid]) == 0
    assert f"Controller  not running — run: llm-bridge resume --mission {mid[:8]}" in capsys.readouterr().out
    assert bridge.main(["mission", "list"]) == 0
    assert "RUNNING (no controller)" in capsys.readouterr().out
    assert bridge.main(["mission", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["missions"][0]
    assert listed["status"] == "RUNNING" and listed["controller_running"] is False
    assert bridge.main(["status", "--mission", mid, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["lifecycle"]["controller_running"] is False

    lock = MissionLock(tmp_path / "state" / "locks", mid)
    lock.acquire()
    try:
        assert bridge.main(["mission", "list"]) == 0
        assert "(no controller)" not in capsys.readouterr().out
        assert bridge.main(["status", "--mission", mid]) == 0
        assert "not running" not in capsys.readouterr().out
    finally:
        lock.release()


def test_format_helpers_flag_only_expected_controllers() -> None:
    assert fmt.no_controller({"status": "PAUSED", "controller_running": False})
    assert not fmt.no_controller({"status": "RUNNING", "controller_running": True})
    assert not fmt.no_controller({"status": "RUNNING"})
    assert not fmt.no_controller({"status": "COMPLETED", "controller_running": False})
    assert "(no controller)" not in fmt.format_mission_list([{"id": "abcdef012345", "status": "STOPPED", "controller_running": False}])


def test_tui_controller_line_reports_missing_controller() -> None:
    snapshot = {"mission": {"status": "RUNNING"}, "lifecycle": {"controller_running": False}}
    assert bridge_tui._controller_line(bridge_tui._bounded_snapshot(snapshot)) == "not running — type resume"
    snapshot["lifecycle"]["controller_running"] = True
    assert bridge_tui._controller_line(bridge_tui._bounded_snapshot(snapshot)) == "no heartbeat recorded yet"
    snapshot["mission"]["status"] = "COMPLETED"
    snapshot["lifecycle"]["controller_running"] = False
    assert bridge_tui._controller_line(bridge_tui._bounded_snapshot(snapshot)) == "mission completed — controller finished"


# --- 7. controller startup tolerates a momentary liveness probe -----------------------------


def test_controller_lock_acquisition_waits_out_a_short_probe(tmp_path: Path) -> None:
    import sys
    import time

    locks = tmp_path / "locks"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time\n"
         "from llm_tools.bridge_store import MissionLock\n"
         "lock = MissionLock(sys.argv[1], 'm1'); lock.acquire()\n"
         "print('held', flush=True); time.sleep(0.1); lock.release()\n",
         str(locks)],
        stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parent.parent,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        controller = MissionLock(locks, "m1")
        with pytest.raises(BlockingIOError):
            controller.acquire()  # default stays non-blocking
        started = time.monotonic()
        controller.acquire(timeout=0.5)
        assert 0.0 < time.monotonic() - started < 0.5
        controller.release()
    finally:
        holder.wait(timeout=scaled(5))


def test_controller_lock_acquisition_gives_up_when_really_owned(tmp_path: Path, monkeypatch) -> None:
    import time
    from llm_tools import bridge_runtime

    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(bridge_runtime, "_LOCK_STARTUP_WAIT_SECONDS", 0.1)
    owner = bridge_runtime._process_controller_lock("m1")
    owner.acquire()
    try:
        started = time.monotonic()
        assert bridge_runtime.run_controller("m1") == 2
        assert 0.1 <= time.monotonic() - started < 1.0
    finally:
        owner.release()


# --- 8-12. keyboard approval, unknown commands, confirmation overlay, status line, idle ----------


class _Screen:
    def __init__(self, height: int, width: int) -> None:
        self.height, self.width = height, width
        self.lines = [""] * height

    def getmaxyx(self):
        return self.height, self.width

    def erase(self) -> None:
        self.lines = [""] * self.height

    def addnstr(self, y, x, text, count, attr=0) -> None:
        line = self.lines[y].ljust(x)
        self.lines[y] = line[:x] + text[:count] + line[x + len(text[:count]):]

    def noutrefresh(self) -> None:
        pass

    def move(self, *_args) -> None:
        pass


def _console(tasks: list[dict], sent: list[str] | None = None):
    sent = [] if sent is None else sent
    state = bridge_tui._ConsoleState("0123456789abcdef0123456789abcdef", lambda _mission, text: sent.append(text) or "{}", lambda *_: {}, full=True)
    state.snapshot = {"mission": {"status": "RUNNING"}, "tasks": tasks}
    return state, sent


def _join_commands() -> None:
    import threading

    for thread in threading.enumerate():
        if thread.name == "llm-bridge-command":
            thread.join(2)


def test_approve_alias_asks_for_confirmation_then_dispatches_the_public_command(monkeypatch) -> None:
    monkeypatch.setattr(bridge_tui.curses, "doupdate", lambda: None)
    state, sent = _console([{"id": 3, "ordinal": 3, "status": "VERIFYING"}, {"id": 4, "ordinal": 4, "status": "QUEUED"}])
    assert state._confirm_or_dispatch("/approve")
    assert state.confirm_command == "task verify --task 3 --approve"
    assert state.output[0] == "Approve verification and mark task 3 complete? · mission 01234567"
    assert "0123456789abcdef0123456789abcdef" not in "\n".join(state.output)
    screen = _Screen(8, 70)
    bridge_tui._draw_response_overlay(screen, state)
    text = "\n".join(screen.lines)
    assert "Y approve · N or Esc cancel" in text and "close" not in text and "scroll" not in text
    assert "mission 01234567" in text and "Task 3:" in text
    state.handle_key("\n")  # Enter neither confirms nor cancels
    assert state.confirm_command is not None and sent == []
    state.handle_key("y")
    _join_commands()
    assert sent == ["task verify --task 3 --approve"]
    state.confirm_command = None
    assert state._confirm_or_dispatch("approve 4")
    assert state.confirm_command == "task verify --task 4 --approve"
    state.handle_key("n")
    assert state.confirm_command is None and state.output[-1] == "Confirmation cancelled."


def test_approve_alias_needs_a_single_candidate_or_a_number() -> None:
    state, _sent = _console([{"id": 1, "status": "VERIFYING"}, {"id": 2, "status": "VERIFYING"}])
    assert not state._confirm_or_dispatch("approve")
    assert state.output[-1] == "error: approve needs a task number · tasks awaiting approval: 1, 2"
    assert not state._confirm_or_dispatch("approve x")
    assert state.output[-1] == "error: usage: approve [TASK-NUMBER]"
    state, _sent = _console([])
    assert not state._confirm_or_dispatch("approve")
    assert state.output[-1].endswith("no task awaits approval")
    assert bridge_tui._expand_console_alias("pause", {}) == ("pause", None)
    assert bridge_tui._expand_console_alias("approve 'unterminated", {}) == ("approve 'unterminated", None)


def test_approve_is_completed_as_a_slash_command() -> None:
    assert bridge_tui._command_completions("/app") == ["/approve"]
    assert "approve" in bridge_tui._command_completions("")
    assert "approve" not in bridge_tui._command_completions("task ")


def test_console_dispatch_approve_expands_to_task_verify(monkeypatch, tmp_path: Path) -> None:
    seen = []
    monkeypatch.setattr(bridge, "_task_result", lambda args, preserve_receipt=False: seen.append((args.task_action, args.task, args.approve)) or {"status": "APPLIED"})
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mission = store.create_mission("Approve", "Objective", cwd=tmp_path)
    mid = mission["id"]
    assert bridge.dispatch_console_command(mid, "/approve 7").startswith("{")
    assert seen == [("verify", 7, True)]
    assert bridge.dispatch_console_command(mid, "approve").endswith("no task awaits approval")
    assert bridge.dispatch_console_command(mid, "approve 1 2") == "error: usage: approve [TASK-NUMBER]"
    monkeypatch.setattr(bridge, "_verifying_task_ids", lambda _mission: [5])
    bridge.dispatch_console_command(mid, "approve")
    assert seen[-1] == ("verify", 5, True)
    monkeypatch.setattr(bridge, "_verifying_task_ids", lambda _mission: [5, 6])
    assert bridge.dispatch_console_command(mid, "approve").endswith("tasks awaiting approval: 5, 6")


def test_unknown_commands_suggest_close_names_instead_of_dumping_choices(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with bridge.open_store() as store:
        mid = store.create_mission("Unknown", "Objective", cwd=tmp_path)["id"]
    reply = bridge.dispatch_console_command(mid, "/aprove 3")
    assert reply.startswith("error: Unknown command 'aprove' · did you mean approve")
    assert reply.endswith("/help lists commands") and "choose from" not in reply
    assert bridge.dispatch_console_command(mid, "stat").startswith("error: Unknown command 'stat' · did you mean start, status")
    assert bridge.dispatch_console_command(mid, "zzzzqq") == "error: Unknown command 'zzzzqq' · /help lists commands"
    nested = bridge.dispatch_console_command(mid, "task lst")
    assert nested.startswith("error: Unknown task command 'lst' · did you mean list")


def test_status_line_is_retired_by_view_change_and_new_commands(monkeypatch) -> None:
    state, _sent = _console([])
    assert state.status_message() is None
    state.output.append("error: old failure")
    assert state.status_message() == "error: old failure"
    state.choose_view(bridge_tui.VIEWS.index("Actions"))
    assert state.status_message() is None
    assert list(state.output) == ["error: old failure"]  # history stays for the response overlay
    state.output.append("Applied: pause")
    assert state.status_message() == "Applied: pause"
    state.dismiss_status()
    assert state.status_message() is None
    assert state._dispatch("pause")
    assert state.status_message() is None  # the status row shows the working indicator instead
    assert bridge_tui._working_text(state).endswith("working…")
    _join_commands()
    state.output.clear()
    state.dismiss_status()
    assert state.status_message() is None
