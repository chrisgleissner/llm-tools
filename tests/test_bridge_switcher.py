"""Mission switcher model, station labels and the /finish command (mocked backends)."""

from __future__ import annotations

import curses
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge, bridge_finish, bridge_switcher as sw
from llm_tools.bridge_mouse import SHIFT, WHEEL_DOWN, WHEEL_UP
from llm_tools.bridge_finish import FinishError, finish_mission
from llm_tools.bridge_store import BridgeStore
from llm_tools.bridge_tui import ControlViewAction, _confirmation_reason, _station_status_context, _station_status_parts
from llm_tools.terminal import TerminalAction, TerminalHandle, TmuxTerminalBackend


def entry(mission_id: str, name: str = "m", status: str = "RUNNING", cwd: str = "/work/repo", **kw: Any) -> sw.MissionEntry:
    return sw.MissionEntry(mission_id=mission_id, name=name, status=status, cwd=cwd, **kw)


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(path))
    return path


# ------------------------------------------------------------------ labels

def test_mission_label_uses_folder_then_name_and_ellipsizes() -> None:
    assert sw.mission_label("Fix tests", "/home/u/dev/c64bridge") == "c64bridge"
    assert sw.mission_label("Fix tests", "/") == "Fix tests"
    long = sw.mission_label("x", "/home/u/dev/a-very-long-repository-name")
    assert long == "a-very-long-r…" and len(long) == 14
    assert sw.mission_label("", "") == ""


def test_status_context_leads_with_mission_label_and_keeps_state_text() -> None:
    snapshot = {
        "mission": {"name": "Fix", "cwd": "/home/u/dev/c64bridge", "status": "RUNNING"},
        "current_task": {"id": 6, "status": "RUNNING"},
    }
    assert _station_status_context(snapshot) == "c64bridge · Task 6 RUNNING"
    snapshot["mission"]["status"] = "PAUSED"
    assert _station_status_context(snapshot) == "c64bridge · Task 6 RUNNING · PAUSED"
    assert _station_status_parts(snapshot) == ("c64bridge", "Task 6 RUNNING · PAUSED")
    assert _station_status_context({"mission": {"status": "RUNNING", "name": "", "cwd": ""}, "current_task": {"id": 1, "status": "RUNNING"}}) == "Task 1 RUNNING"


# ------------------------------------------------------------------ ordering and filtering

def sample_entries() -> list[sw.MissionEntry]:
    return [
        entry("a" * 32, "Old done", "COMPLETED", "/work/done", updated_at="2026-01-01"),
        entry("b" * 32, "Idle stopped", "STOPPED", "/work/stopped", updated_at="2026-03-01"),
        entry("c" * 32, "Live older", "RUNNING", "/work/older", station=True, controller=True, provider="claude", task_id=2, task_status="RUNNING", task_title="Write docs", updated_at="2026-02-01"),
        entry("d" * 32, "Live newer", "RUNNING", "/work/newer", controller=True, provider="codex", updated_at="2026-04-01"),
    ]


def test_order_puts_live_first_then_active_then_finished() -> None:
    ordered = sw.order_entries(sample_entries())
    assert [e.name for e in ordered] == ["Live newer", "Live older", "Idle stopped", "Old done"]


def test_filter_matches_every_term_case_insensitively() -> None:
    picker = sw.MissionPicker(sw.order_entries(sample_entries()))
    assert len(picker.visible()) == 4
    for char in "CLAUDE":
        picker.key(char)
    assert [e.name for e in picker.visible()] == ["Live older"]
    picker.key("\x15")
    for char in "live docs":
        picker.key(char)
    assert [e.name for e in picker.visible()] == ["Live older"]
    picker.key("\x15")
    for char in "nomatch":
        picker.key(char)
    assert picker.visible() == [] and picker.key("\n") is None
    rendered = "\n".join(text for text, _ in picker.lines(24, 80))
    assert "No missions match 'nomatch'" in rendered


def test_navigation_enter_and_cancel_keys() -> None:
    picker = sw.MissionPicker(sw.order_entries(sample_entries()))
    picker.lines(24, 80)
    picker.key("KEY_DOWN"); picker.key(curses.KEY_DOWN)
    assert picker.selected == 2
    picker.key("\x10"); picker.key("KEY_UP")
    assert picker.selected == 0
    picker.key("KEY_END"); assert picker.selected == 3
    picker.key("KEY_HOME"); assert picker.selected == 0
    picker.key("KEY_NPAGE"); assert picker.selected == 3
    picker.key("KEY_PPAGE"); assert picker.selected == 0
    picker.key("x"); picker.key("\x7f")
    assert picker.query == ""
    assert picker.key("\n") == "select" and picker.chosen is picker.visible()[0]
    esc = sw.MissionPicker(sample_entries())
    assert esc.key("\x1b") == "cancel" and esc.cancelled and esc.chosen is None


def test_enter_maps_to_attach_for_station_and_controls_otherwise() -> None:
    live = entry("c" * 32, station=True)
    stopped = entry("b" * 32, "Stopped", "STOPPED")
    assert sw.action_for(live, "z") == TerminalAction("c" * 32, None, "attach")
    assert sw.action_for(live, "c" * 32) == TerminalAction("c" * 32, None, "attach")
    assert sw.action_for(stopped, "z") == ControlViewAction("b" * 32, "Bridge")
    assert sw.action_for(stopped, "b" * 32) is None
    controller_only = entry("d" * 32, controller=True)
    assert sw.action_for(controller_only, "z") == ControlViewAction("d" * 32, "Bridge")


# ------------------------------------------------------------------ rendering

def test_render_fits_80x24_with_columns_and_markers() -> None:
    picker = sw.MissionPicker(sw.order_entries(sample_entries()), current="c" * 32, home="/work")
    lines = picker.lines(24, 80)
    assert len(lines) == 24
    for text, _style in lines:
        assert sw.cell_width(text) <= 80
    text = "\n".join(t for t, _ in lines)
    assert "Missions · 4 of 4 · 2 live" in text
    assert "▶" in text and "●" in text and "›" in text and "▌" in text  # this-mission marker, live marker, cursor, mission chip
    assert "● RUNNING" in text and "■" in text  # state glyph with its word; provider square
    assert "~/older" in text and "claude" in text and "2 Write docs" in text
    assert text.splitlines()[-1].startswith("Type to filter")
    assert "station running" in text or "controller running" in text


def test_render_small_and_scrolling_windows() -> None:
    many = [entry(f"{i:032d}", f"Mission {i}", updated_at=f"2026-01-{i % 28 + 1:02d}") for i in range(60)]
    picker = sw.MissionPicker(many)
    lines = picker.lines(12, 60)
    assert len(lines) == 12 and all(sw.cell_width(t) <= 60 for t, _ in lines)
    for _ in range(30):
        picker.key("KEY_DOWN")
    shown = "\n".join(t for t, _ in picker.lines(12, 60))
    assert picker.selected == 30 and picker.top > 0
    assert sum(1 for t, s in picker.lines(12, 60) if s == "sel") == 1
    assert "Mission" in shown
    assert sw.MissionPicker([]).lines(24, 80)[3][0].startswith("No missions yet")


def test_render_is_ascii_only_in_non_utf8_locale() -> None:
    assert sw.unicode_ok("UTF-8") and sw.unicode_ok("utf8")
    assert not sw.unicode_ok("ANSI_X3.4-1968") and not sw.unicode_ok("ISO-8859-1")
    assert isinstance(sw.unicode_ok(), bool)
    rows = [entry("e" * 32, "Überprüfung ✓ 日本語", cwd="/work/répertoire", station=True, provider="claude", task_id=1, task_title="Fix – ünïcode")]
    picker = sw.MissionPicker(rows, current="e" * 32, unicode=False)
    text = "\n".join(t for t, _ in picker.lines(24, 80))
    text.encode("ascii")
    assert "@" in text and ">" in text and "|" in text
    narrow = "\n".join(t for t, _ in sw.MissionPicker(rows, unicode=False).lines(24, 40))
    narrow.encode("ascii")
    assert "~" in narrow


def test_text_helpers() -> None:
    assert sw.clip("a\tb\nc", 10) == "a b c"
    assert sw.clip("abcdef", 4) == "abc…" and sw.clip("abcdef", 4, unicode=False) == "abc~"
    assert sw.clip("abc", 1) == "…" and sw.clip("abc", 0) == ""
    assert sw.short_home("/h/u/x", "/h/u") == "~/x" and sw.short_home("/h/u", "/h/u") == "~" and sw.short_home("/o", "/h/u") == "/o"
    assert sw.short_home("/home/x")
    assert sw.middle_clip("/very/long/path/to/some/place", 12) .count("…") == 1
    assert sw.middle_clip("abc", 10) == "abc" and sw.middle_clip("abcdefgh", 1) == "…"
    assert sw.fit_columns([5, 5, 5], 100) == [5, 5, 5]
    assert sum(sw.fit_columns([40, 40, 40], 30)) <= 30


def test_plain_listing_and_json_shape() -> None:
    rows = sw.order_entries(sample_entries())
    text = sw.format_missions(rows)
    assert text.splitlines()[0] == "4 missions · 2 live"
    assert "Live older" in text and "ID" in text.splitlines()[1]
    assert sw.format_missions([]).startswith("No missions yet")
    payload = sw.missions_json(rows)
    assert payload["schema_version"] == 1 and len(payload["missions"]) == 4
    first = payload["missions"][0]
    assert set(first) == {"id", "short_id", "name", "status", "cwd", "provider", "current_task", "live", "station", "station_idle", "stale", "review_task", "controller_running", "rotation", "colour"}
    assert first["colour"].startswith("mission_")
    assert payload["missions"][1]["current_task"] == {"id": 2, "ordinal": 2, "status": "RUNNING", "title": "Write docs"}
    assert first["provider"] == "codex" and payload["missions"][2]["provider"] is None


# ------------------------------------------------------------------ store collection and CLI

def make_missions(state: Path, tmp_path: Path) -> tuple[str, str]:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    with BridgeStore(state / "bridge.sqlite3") as store:
        first = store.create_mission("First", "do one", cwd=tmp_path / "one", initial_prompt="do one", config={"providers": "claude,codex"})
        second = store.create_mission("Second", "do two", cwd=tmp_path / "two", initial_prompt="do two", config={
            "routes": "mini", "frozen_tool_config": '[routes.mini]\nprovider = "kilo"\n'})
        store.set_mission_status(second["id"], "RUNNING")
        operation = store.begin_operation(int(second["initial_task_id"]), iteration=1, station="mini")
        store.mark_operation_running(operation, route_id="mini")
    return str(first["id"]), str(second["id"])


def test_collect_entries_reads_provider_task_and_liveness(state: Path, tmp_path: Path) -> None:
    first, second = make_missions(state, tmp_path)
    with BridgeStore(state / "bridge.sqlite3") as store:
        rows = sw.collect_entries(store, station_probe=lambda m: m == first, controller_probe=lambda m: False)
    by_id = {r.mission_id: r for r in rows}
    assert rows[0].mission_id == first and rows[0].station
    assert by_id[second].provider == "kilo" and by_id[second].task_id is not None
    assert by_id[first].task_status == "READY" or by_id[first].task_status == ""
    assert by_id[first].rotation == "claude,codex"


def test_default_probes_are_false_for_unknown_missions(state: Path) -> None:
    assert sw.default_station_probe("nope") is False
    assert sw.default_controller_probe("nope") is False
    assert sw.provider_of_station({}, "") == "-"
    assert sw.provider_of_station({"frozen_tool_config": "not = [toml"}, "codex") == "codex"


def test_missions_cli_lists_and_emits_json(state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first, _second = make_missions(state, tmp_path)
    assert bridge.main(["missions"]) == 0
    out = capsys.readouterr().out
    assert "2 missions" in out and first[:8] in out
    assert bridge.main(["missions", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload["missions"]] and payload["schema_version"] == 1
    assert bridge.main(["missions", "--pick"]) == 2  # no terminal under pytest
    assert "interactive terminal" in capsys.readouterr().err


def test_console_dispatch_missions_returns_pick_action_and_finish_errors(state: Path, tmp_path: Path) -> None:
    first, _ = make_missions(state, tmp_path)
    assert bridge.dispatch_console_command(first, "/missions") == TerminalAction(first, None, "pick")
    assert bridge.dispatch_console_command(first, "missions") == TerminalAction(first, None, "pick")
    reply = bridge.dispatch_console_command(first, "/finish")
    assert reply.startswith("error: Finish applies to native providers only")
    assert "finish" in bridge.dispatch_console_command(first, "help finish").lower()
    assert _confirmation_reason("/finish") and _confirmation_reason("finish --mission x")


# ------------------------------------------------------------------ finish

class FakeBackend:
    """Records keys; the provider 'exits' after a chosen number of Enter presses."""

    IDLE = "some earlier output\n\n› \n  gpt-5 · ~/work\n"
    POPUP = "› /quit\n  /quit   Exit Codex\n  /quote  Something else\n"

    def __init__(self, exit_after_enters: int | None = 1, alive_after: bool = False, screen: str | None = None, popup: bool = True) -> None:
        self.screen = self.IDLE if screen is None else screen
        self.popup = popup
        self.sent: list[str] = []
        self.exit_after = exit_after_enters
        self.alive = True
        self.session = True
        self.on_exit = lambda: None

    def pane_is_alive(self, handle: Any, *, pane: str) -> bool:
        return self.alive

    def session_exists(self, handle: Any) -> bool:
        return self.session

    def capture(self, handle: Any, *, pane: str) -> str:
        return self.screen

    def send_keys(self, handle: Any, *, pane: str, keys: str) -> None:
        self.sent.append(keys)
        if keys == "Enter" and self.popup and self.sent.count("Enter") == 1:
            self.screen = self.POPUP.replace("/quit", self.sent[0]) if self.sent[0] != "/quit" else self.POPUP
        if self.exit_after is not None and self.sent.count("Enter") >= self.exit_after:
            self.alive = False
            self.on_exit()


def running_native(state: Path, tmp_path: Path, provider: str = "codex", native: bool = True, headless: bool = False, routes: str = "") -> tuple[BridgeStore, str, int, str]:
    folder = tmp_path / f"w-{provider}-{native}"
    folder.mkdir()
    store = BridgeStore(state / "bridge.sqlite3")
    config: dict[str, Any] = {"providers": provider, "native_tui": native, "headless": headless}
    if routes:
        config.update(routes=routes, frozen_tool_config=f'[routes.{routes}]\nprovider = "{provider}"\n')
    mission = store.create_mission("Native", "obj", cwd=folder, initial_prompt="obj", config=config)
    mission_id, task_id = str(mission["id"]), int(mission["initial_task_id"])
    store.set_mission_status(mission_id, "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station=routes or provider)
    store.mark_operation_running(operation, route_id=routes)
    return store, mission_id, task_id, operation


HANDLE = TerminalHandle(Path("/tmp/x.sock"), Path("/tmp/x.conf"), "mission-x", "%0", "%1", 6)


@pytest.mark.parametrize(("provider", "command"), [("claude", "/exit"), ("codex", "/quit"), ("copilot", "/exit")])
def test_finish_sends_provider_exit_command_then_second_enter(state: Path, tmp_path: Path, provider: str, command: str) -> None:
    store, mission_id, task_id, operation = running_native(state, tmp_path, provider)
    backend = FakeBackend(exit_after_enters=2)

    def finish_operation() -> None:
        store.finish_operation(operation, exit_code=0)

    backend.on_exit = finish_operation
    result = finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, sleep=lambda _s: None)
    assert backend.sent == [command, "Enter", "Enter"]
    assert result.provider == provider and result.command == command and result.task_id == task_id
    assert result.task_status == "VERIFYING" and "task" in result.detail
    assert result.as_json()["outcome"] == "exited"


def test_finish_skips_second_enter_when_pane_already_dead(state: Path, tmp_path: Path) -> None:
    store, mission_id, _task, operation = running_native(state, tmp_path, "claude")
    backend = FakeBackend(exit_after_enters=1)
    backend.on_exit = lambda: store.finish_operation(operation, exit_code=0)
    finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, sleep=lambda _s: None)
    assert backend.sent == ["/exit", "Enter"]


def test_finish_resolves_route_provider_and_honours_override(state: Path, tmp_path: Path) -> None:
    store, mission_id, _task, operation = running_native(state, tmp_path, "codex", routes="my-route")
    backend = FakeBackend(exit_after_enters=2)
    backend.on_exit = lambda: store.finish_operation(operation, exit_code=0)
    finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, sleep=lambda _s: None, env={"LLM_BRIDGE_FINISH_CODEX": "/exit"})
    assert backend.sent[0] == "/exit"
    assert bridge_finish.exit_command("kilo", {}) is None
    assert bridge_finish.exit_command("kilo", {"LLM_BRIDGE_FINISH_KILO": "/bye"}) == "/bye"


def test_finish_success_when_session_vanishes(state: Path, tmp_path: Path) -> None:
    store, mission_id, _task, _op = running_native(state, tmp_path, "codex")
    backend = FakeBackend(exit_after_enters=None)
    original = backend.send_keys

    def vanish(handle: Any, *, pane: str, keys: str) -> None:
        original(handle, pane=pane, keys=keys)
        if keys == "Enter":
            backend.session = False
            raise RuntimeError("no server running")

    backend.send_keys = vanish  # type: ignore[method-assign]
    result = finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, sleep=lambda _s: None)
    assert result.provider == "codex"


def test_finish_times_out_without_killing(state: Path, tmp_path: Path) -> None:
    store, mission_id, task_id, _op = running_native(state, tmp_path, "codex")
    backend = FakeBackend(exit_after_enters=None)
    ticks = iter(range(0, 1000))
    with pytest.raises(FinishError, match=r"Provider did not exit · switch to it with Ctrl-\] then P"):
        finish_mission(mission_id, backend=backend, handle=HANDLE, store=store, timeout=10, sleep=lambda _s: None, clock=lambda: float(next(ticks)))
    assert backend.sent == ["/quit", "Enter", "Enter"]
    assert store.get_task(task_id)["status"] == "RUNNING"


def test_finish_refuses_headless_missing_provider_and_unknown_exit(state: Path, tmp_path: Path) -> None:
    store, mission_id, _t, _o = running_native(state, tmp_path, "codex", native=False)
    with pytest.raises(FinishError, match="native providers only"):
        finish_mission(mission_id, backend=FakeBackend(), handle=HANDLE, store=store)
    (tmp_path / "x").mkdir()
    store2, mission2, _t2, _o2 = running_native(state, tmp_path / "x", "codex", native=True, headless=True)
    with pytest.raises(FinishError, match="native providers only"):
        finish_mission(mission2, backend=FakeBackend(), handle=HANDLE, store=store2)
    (tmp_path / "y").mkdir()
    store3, mission3, _t3, op3 = running_native(state, tmp_path / "y", "kilo")
    with pytest.raises(FinishError, match="No exit command is known for kilo"):
        finish_mission(mission3, backend=FakeBackend(), handle=HANDLE, store=store3, env={})
    store3.finish_operation(op3, exit_code=0)
    with pytest.raises(FinishError, match="nothing to finish"):
        finish_mission(mission3, backend=FakeBackend(), handle=HANDLE, store=store3)


def test_finish_refuses_dead_pane_and_missing_station(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, mission_id, _t, _o = running_native(state, tmp_path, "claude")
    dead = FakeBackend()
    dead.alive = False
    with pytest.raises(FinishError, match="pane is not running"):
        finish_mission(mission_id, backend=dead, handle=HANDLE, store=store)
    with pytest.raises(FinishError, match="No provider station is running"):
        finish_mission(mission_id, backend=FakeBackend(), store=store)


def test_cli_finish_reports_json_and_errors(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    store, mission_id, task_id, _op = running_native(state, tmp_path, "codex")
    result = bridge_finish.FinishResult(mission_id, "codex", task_id, "/quit", "VERIFYING", "Provider exited · task 1 is now VERIFYING")
    monkeypatch.setattr(bridge_finish, "finish_mission", lambda _id, **_k: result)
    assert bridge.main(["finish", "--mission", mission_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "exited" and payload["schema_version"] == 1 and payload["exit_command"] == "/quit"
    assert bridge.main(["finish", "--mission", mission_id]) == 0
    assert capsys.readouterr().out.strip() == result.detail
    assert bridge.dispatch_console_command(mission_id, "/finish").startswith('{"status": "APPLIED"')

    def refuse(_id: str, **_k: Any) -> Any:
        raise FinishError(bridge_finish.NOT_EXITED_MESSAGE)

    monkeypatch.setattr(bridge_finish, "finish_mission", refuse)
    assert bridge.main(["finish", "--mission", mission_id]) == 2
    assert "Provider did not exit" in capsys.readouterr().err
    assert bridge.dispatch_console_command(mission_id, "finish") == "error: Provider did not exit · switch to it with Ctrl-] then P"


# ------------------------------------------------------------------ pick wiring

def test_pick_mission_action_and_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [entry("a" * 32, station=True), entry("b" * 32, "Other", station=True), entry("c" * 32, "Idle", "STOPPED")]
    assert sw.pick_mission_action("a" * 32, entries=rows, chooser=lambda *_a, **_k: None) is None
    assert sw.pick_mission_action("a" * 32, entries=rows, chooser=lambda *_a, **_k: rows[1]) == TerminalAction("b" * 32, None, "attach")
    assert sw.pick_mission_action("a" * 32, entries=rows, chooser=lambda *_a, **_k: rows[2]) == ControlViewAction("c" * 32, "Bridge")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(sw, "_client_for", lambda _m: "/dev/pts/9")
    monkeypatch.setattr(sw, "_expand_bridge_pane", lambda _m: False)
    picked = sw.pick_mission_action("a" * 32, entries=rows, chooser=lambda *_a, **_k: rows[1])
    assert picked == TerminalAction("b" * 32, "/dev/pts/9", "attach")


def test_pick_expands_and_restores_compact_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(sw, "_expand_bridge_pane", lambda _m: calls.append("expand") or True)
    monkeypatch.setattr(sw, "_restore_bridge_pane", lambda _m: calls.append("restore"))
    assert sw.pick_mission_action("a" * 32, entries=[entry("a" * 32)], chooser=lambda *_a, **_k: None) is None
    assert calls == ["expand", "restore"]


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def switch_client_to(self, source: Any, destination: Any, *, client_name: str | None = None) -> None:
        self.calls.append(("switch", source.session_name, destination.session_name, str(client_name)))

    def focus(self, handle: Any, *, pane: str, expand: bool = False) -> None:
        self.calls.append(("focus", handle.session_name, pane, str(expand)))

    def active_client(self, handle: Any) -> str | None:
        return "/dev/pts/7"


def test_run_pick_cli_switches_focuses_or_opens_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [entry("a" * 32, station=True), entry("b" * 32, station=True), entry("c" * 32, status="STOPPED")]
    backend = RecordingBackend()
    monkeypatch.setattr("llm_tools.terminal.TmuxTerminalBackend", lambda: backend)
    monkeypatch.setattr(bridge, "_load_handle", lambda mission: TerminalHandle(Path("/tmp/s"), Path("/tmp/s.conf"), f"mission-{mission}", "%0", "%1", 6))
    assert sw.run_pick_cli("a" * 32, None, entries=rows, chooser=lambda *_a, **_k: None) == 0
    assert sw.run_pick_cli("a" * 32, "/dev/pts/3", entries=rows, chooser=lambda *_a, **_k: rows[1]) == 0
    assert backend.calls[-1] == ("switch", "mission-" + "a" * 32, "mission-" + "b" * 32, "/dev/pts/3")
    assert sw.run_pick_cli("a" * 32, None, entries=rows, chooser=lambda *_a, **_k: rows[1]) == 0
    assert backend.calls[-1][3] == "/dev/pts/7"
    assert sw.run_pick_cli("a" * 32, None, entries=rows, chooser=lambda *_a, **_k: rows[0]) == 0
    assert backend.calls[-1] == ("focus", "mission-" + "a" * 32, "provider", "True")


def test_run_pick_cli_opens_controls_without_tmux_env(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setenv("TMUX", "/tmp/t,1,0")
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setattr("llm_tools.bridge_tui.run_control", lambda mission, *_a, **_k: opened.append(mission) or 0)
    rows = [entry("c" * 32, status="STOPPED")]
    assert sw.run_pick_cli("a" * 32, None, entries=rows, chooser=lambda *_a, **_k: rows[0]) == 0
    assert opened == ["c" * 32]
    import os
    assert "TMUX" not in os.environ
    assert sw.run_pick_cli("c" * 32, None, entries=rows, chooser=lambda *_a, **_k: rows[0]) == 0
    assert opened == ["c" * 32]


def test_run_pick_cli_standalone_attach_uses_existing_attach_path(monkeypatch: pytest.MonkeyPatch) -> None:
    performed: list[tuple[str, TerminalAction]] = []
    monkeypatch.setattr("llm_tools.bridge_tui._execute_terminal_action", lambda mission, action: performed.append((mission, action)))
    monkeypatch.setattr(bridge, "_load_handle", lambda mission: TerminalHandle(Path("/tmp/s"), Path("/tmp/s.conf"), f"mission-{mission}", "%0", "%1", 6))
    monkeypatch.setattr("llm_tools.terminal.TmuxTerminalBackend", lambda: RecordingBackend())
    rows = [entry("b" * 32, station=True)]
    assert sw.run_pick_cli(None, None, entries=rows, chooser=lambda *_a, **_k: rows[0]) == 0
    assert performed == [("b" * 32, TerminalAction("b" * 32, None, "attach"))]


def test_curses_runner_draws_and_returns_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    class Window:
        def __init__(self) -> None:
            self.keys = iter(["x", "\x7f", curses.KEY_DOWN, "\n"])
            self.drawn: list[str] = []

        def keypad(self, _f: bool) -> None: ...
        def timeout(self, _ms: int) -> None: ...
        def getmaxyx(self) -> tuple[int, int]: return (24, 80)
        def erase(self) -> None: ...
        def refresh(self) -> None: ...
        def addstr(self, _y: int, _x: int, text: str, _attr: int = 0) -> None: self.drawn.append(text)
        def addnstr(self, _y: int, _x: int, text: str, _n: int, _attr: int = 0) -> None: self.drawn.append(text)

        def get_wch(self) -> object:
            try:
                return next(self.keys)
            except StopIteration:
                raise curses.error()

    window = Window()
    monkeypatch.setattr(curses, "wrapper", lambda fn: fn(window))
    monkeypatch.setattr(curses, "use_env", lambda _flag: None)
    monkeypatch.setattr(curses, "curs_set", lambda _n: (_ for _ in ()).throw(curses.error()))
    monkeypatch.setattr("llm_tools.bridge_tui.sync_window_size", lambda _w: None)
    rows = sw.order_entries(sample_entries())
    chosen = sw.run_picker(rows, current=None, unicode=True)
    assert chosen is not None and chosen.name == "Live older"
    assert any("Filter:" in text for text in window.drawn)


# ------------------------------------------------------------------ terminal

def test_terminal_active_client_tmux_version_and_fallback_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import terminal

    real_version = terminal.tmux_version
    backend = TmuxTerminalBackend()
    monkeypatch.setattr(backend, "_require_owned", lambda _h: None)
    listing = "100\t/dev/pts/1\t0\t0\n250\t/dev/pts/2\t0\t0\n999\t/dev/pts/3\t1\t0\nbad\t/dev/pts/4\t0\t0\n"
    monkeypatch.setattr(backend, "_run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=listing))
    assert backend.active_client(HANDLE) == "/dev/pts/2"
    monkeypatch.setattr(backend, "_run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=""))
    assert backend.active_client(HANDLE) is None

    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(backend, "_run", lambda _s, *args, **_k: runs.append(args) or subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(terminal, "tmux_version", lambda: (3, 1))
    backend._bind_mission_switcher(Path("/tmp/s"), "%1", Path("/work"), ["python", "-m", "x"])
    assert any("send-keys" in args and "/missions" in args for args in runs)
    assert not any("display-popup" in args for args in runs)
    runs.clear()
    monkeypatch.setattr(terminal, "tmux_version", lambda: (3, 2))
    backend._bind_mission_switcher(Path("/tmp/s"), "%1", Path("/work"), ["python", "-m", "x"])
    popup = next(args for args in runs if "display-popup" in args)
    assert "-B" not in popup and popup[-1] == "python -m x"
    runs.clear()
    monkeypatch.setattr(terminal, "tmux_version", lambda: (3, 6))
    backend._bind_mission_switcher(Path("/tmp/s"), "%1", Path("/work"), ["python", "-m", "x"])
    assert "-B" in next(args for args in runs if "display-popup" in args)
    runs.clear()
    backend._bind_mission_switcher(Path("/tmp/s"), "%1", Path("/work"), None)
    assert not any("display-popup" in args for args in runs)

    monkeypatch.setattr(terminal.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="tmux next-3.5a\n"))
    assert real_version() == (3, 5)
    monkeypatch.setattr(terminal.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="garbage"))
    assert real_version() is None
    monkeypatch.setattr(terminal.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert real_version() is None


def test_status_context_sanitizer_keeps_ellipsis_and_middle_dot(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = TmuxTerminalBackend()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(backend, "_require_owned", lambda _h: None)
    monkeypatch.setattr(backend, "_run", lambda _s, *args, **_k: calls.append(args) or subprocess.CompletedProcess([], 0))
    backend.set_status_label(HANDLE, "a-very-long-r…")
    backend.set_status_context(HANDLE, "Task 6 RUNNING")
    assert calls == [
        ("set-option", "-g", "@llm_bridge_label", "a-very-long-r…"),
        ("set-option", "-g", "@llm_bridge_context", "Task 6 RUNNING"),
    ]
    calls.clear()
    backend.set_status_label(HANDLE, "")
    assert calls[-1][-2:] == ("@llm_bridge_label", "")


def test_cli_help_lists_new_commands() -> None:
    result = subprocess.run([sys.executable, "-m", "llm_tools.bridge", "--help"], capture_output=True, text=True, check=True)
    assert "missions" in result.stdout and "finish" in result.stdout
    sub = subprocess.run([sys.executable, "-m", "llm_tools.bridge", "finish", "--help"], capture_output=True, text=True, check=True)
    assert "--mission" in sub.stdout and "--json" in sub.stdout


# ------------------------------------------------------------------ mouse

def test_wheel_moves_selection_and_click_selects_double_click_opens() -> None:
    rows = sw.order_entries([entry(f"{i:032d}", f"Mission {i}", updated_at=f"2026-01-{i + 1:02d}") for i in range(12)])
    picker = sw.MissionPicker(rows)
    picker.lines(24, 80)
    sw.mouse_outcome(picker, 5, WHEEL_DOWN)
    assert picker.selected == 3
    sw.mouse_outcome(picker, 5, WHEEL_DOWN)
    sw.mouse_outcome(picker, 5, WHEEL_DOWN)
    sw.mouse_outcome(picker, 5, WHEEL_DOWN)
    assert picker.selected == 11  # clamped to the last mission
    sw.mouse_outcome(picker, 5, WHEEL_UP)
    assert picker.selected == 8
    picker.lines(24, 80)
    assert sw.mouse_outcome(picker, 3 + 2, curses.BUTTON1_CLICKED) is None
    assert picker.selected == 2 and picker.chosen is None
    assert sw.mouse_outcome(picker, 3 + 4, curses.BUTTON1_DOUBLE_CLICKED) == "select"
    assert picker.chosen is rows[4]
    # Header rows, rows past the list and unrelated buttons do nothing.
    other = sw.MissionPicker(rows)
    other.lines(24, 80)
    assert sw.mouse_outcome(other, 0, curses.BUTTON1_CLICKED) is None and other.selected == 0
    assert sw.mouse_outcome(other, 3 + 20, curses.BUTTON1_DOUBLE_CLICKED) is None
    assert sw.mouse_outcome(other, 4, curses.BUTTON3_CLICKED) is None
    # Shift+wheel is horizontal and does not move the list.
    before = other.selected
    assert sw.mouse_outcome(other, 4, WHEEL_DOWN | SHIFT) is None and other.selected == before


def test_click_maps_rows_through_scrolled_window() -> None:
    rows = sw.order_entries([entry(f"{i:032d}", f"Mission {i}") for i in range(40)])
    picker = sw.MissionPicker(rows)
    for _ in range(25):
        picker.key("KEY_DOWN")
    picker.lines(12, 60)
    top = picker.top
    assert top > 0
    picker.click(3 + 1)
    assert picker.selected == top + 1


def test_curses_runner_handles_mouse_events(monkeypatch: pytest.MonkeyPatch) -> None:
    events = iter([(0, 0, 4, 0, WHEEL_DOWN), (0, 0, 5, 0, curses.BUTTON1_DOUBLE_CLICKED)])

    class Window:
        def __init__(self) -> None:
            self.keys = iter([curses.KEY_MOUSE, curses.KEY_MOUSE, curses.KEY_MOUSE])

        def keypad(self, _f: bool) -> None: ...
        def timeout(self, _ms: int) -> None: ...
        def getmaxyx(self) -> tuple[int, int]: return (24, 80)
        def erase(self) -> None: ...
        def refresh(self) -> None: ...
        def addstr(self, *_a: object) -> None: ...
        def addnstr(self, *_a: object) -> None: ...
        def get_wch(self) -> object: return next(self.keys)

    def getmouse() -> tuple[int, int, int, int, int]:
        try:
            return next(events)
        except StopIteration:
            raise curses.error() from None

    masks: list[int] = []
    monkeypatch.setattr(curses, "wrapper", lambda fn: fn(Window()))
    monkeypatch.setattr(curses, "use_env", lambda _flag: None)
    monkeypatch.setattr(curses, "curs_set", lambda _n: None)
    monkeypatch.setattr(curses, "mousemask", lambda mask: masks.append(mask))
    monkeypatch.setattr(curses, "getmouse", getmouse)
    monkeypatch.setattr("llm_tools.bridge_tui.sync_window_size", lambda _w: None)
    rows = [entry(f"{i:032d}", f"M{i}", updated_at=f"2026-01-{i + 1:02d}") for i in range(8)]
    chosen = sw.run_picker(rows, unicode=True)
    assert masks and masks[0] & curses.ALL_MOUSE_EVENTS and not masks[0] & curses.REPORT_MOUSE_POSITION
    # A double click on screen row 5 opens the third listed mission.
    assert chosen is rows[2]
