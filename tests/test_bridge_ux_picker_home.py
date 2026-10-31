"""Review state, picker liveness, station gc, picker header and home-form fixes (fake curses windows, no providers)."""

from __future__ import annotations

import curses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from llm_tools import bridge, bridge_launcher as launcher, bridge_runtime, bridge_switcher as sw, bridge_tui
from llm_tools.bridge_store import BridgeStore
from llm_tools.bridge_launcher import Launcher

from .test_bridge_launcher_home import Grid


@pytest.fixture(autouse=True)
def _terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(bridge_tui, "initialize_colors", lambda: False)
    monkeypatch.setattr(launcher, "provider_installed", lambda name: True)


@pytest.fixture()
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(path))
    return path


def entry(mission_id: str, name: str = "m", status: str = "RUNNING", cwd: str = "/work/repo", **kw: Any) -> sw.MissionEntry:
    return sw.MissionEntry(mission_id=mission_id * 32, name=name, status=status, cwd=cwd, **kw)


def review_mission(state: Path, tmp_path: Path, status: str = "RUNNING") -> tuple[str, int]:
    """A mission whose second task awaits review; returns the mission ID and that task's number."""
    folder = tmp_path / "repo"
    folder.mkdir(exist_ok=True)
    with BridgeStore(state / "bridge.sqlite3") as store:
        created = store.create_mission("Reviewed", "obj", cwd=folder, initial_prompt="obj", config={"providers": "codex"})
        mission_id = str(created["id"])
        second = store.add_task(mission_id, "second task", title="Second")
        store.set_mission_status(mission_id, "RUNNING")
        if status != "RUNNING":
            store.set_mission_status(mission_id, status)
        store.db.execute("UPDATE tasks SET status='VERIFYING' WHERE id=?", (second,))
        store.db.commit()
        return mission_id, int(store.get_task(second)["ordinal"])


# --- 1: review state ----------------------------------------------------------------------------

def test_home_list_shows_review_instead_of_running_with_a_detail_line(state: Path, tmp_path: Path) -> None:
    mission_id, ordinal = review_mission(state, tmp_path)
    missions, stats = launcher._load_home(str(tmp_path))
    assert stats[mission_id].review == ordinal == 2
    home = Launcher(str(tmp_path), missions=missions, stats=stats, selected=1, installed=("codex",), probe=lambda _ident: True)
    grid = Grid(size=(34, 120))
    launcher._draw(grid, home)
    row = next(line for line in grid.text().splitlines() if mission_id[:8] in line)
    assert "◆ REVIEW" in row and "RUNNING" not in row
    assert "task 2 awaits your review · Enter to open" in grid.text()
    parts = launcher._mission_row(home, missions[0], launcher.plan_columns(missions, stats, 120), "live", 0, False, 0.0)
    assert dict((text.strip(), role) for text, role in parts)["◆ REVIEW"] == "state_review"


def test_home_review_applies_to_running_missions_only(state: Path, tmp_path: Path) -> None:
    mission_id, _ = review_mission(state, tmp_path, status="PAUSED")
    missions, stats = launcher._load_home(str(tmp_path))
    home = Launcher(str(tmp_path), missions=missions, stats=stats, selected=1, installed=("codex",), probe=lambda _ident: True)
    assert home.review_task(missions[0]) is None
    grid = Grid(size=(34, 120))
    launcher._draw(grid, home)
    assert "REVIEW" not in grid.text() and mission_id[:8] in grid.text()
    # The periodic read-only refresh sees the same review state.
    assert launcher._refresh_home(str(tmp_path))[1][mission_id].review == 2


def test_picker_shows_review_state_and_detail_line(state: Path, tmp_path: Path) -> None:
    mission_id, _ = review_mission(state, tmp_path)
    with BridgeStore(state / "bridge.sqlite3") as store:
        rows = sw.collect_entries(store, station_probe=lambda _m: False, controller_probe=lambda _m: True)
    assert rows[0].review_ordinal == 2 and rows[0].display_status == "REVIEW" and rows[0].as_json()["review_task"] == 2
    picker = sw.MissionPicker([*rows, entry("z", "Other", "COMPLETED")], home=str(tmp_path.parent))
    detail = picker.segment_lines(24, 80)[-2][0]
    assert "".join(text for text, _role in detail).startswith("task 2 awaits your review · Enter to open")
    assert detail[0][1] == "state_review"
    picker.key("KEY_DOWN")  # the selected row is drawn as one bar; look at the review row unselected
    row = next(line for line, _style in picker.segment_lines(24, 80) if any(mission_id[:8] in text for text, _role in line))
    assert ("◆ REVIEW   ", "state_review") in row
    paused = [sw.MissionEntry(**{**rows[0].__dict__, "status": "PAUSED"})]
    assert paused[0].display_status == "PAUSED" and paused[0].as_json()["review_task"] is None


# --- 2: picker liveness, station idle, close, gc ----------------------------------------------------

def liveness_entries() -> list[sw.MissionEntry]:
    return [
        entry("a", "Finished with station", "COMPLETED", station=True),
        entry("b", "Running with controller", controller=True, station=True),
        entry("c", "Stale running", "RUNNING"),
        entry("d", "Failed with station", "FAILED", station=True),
        entry("e", "Cancelled", "CANCELLED", station=True, controller=True),
    ]


def test_live_counts_only_unfinished_missions_with_a_controller_or_station() -> None:
    rows = liveness_entries()
    assert [r.live for r in rows] == [False, True, False, False, False]
    assert [r.station_idle for r in rows] == [True, False, False, True, True]
    picker = sw.MissionPicker(sw.order_entries(rows))
    assert picker.lines(24, 100)[0][0].startswith("Missions · 5 of 5 · 1 live")
    assert sw.format_missions(sw.order_entries(rows)).startswith("5 missions · 1 live")
    assert sw.order_entries(rows)[0].name == "Running with controller"
    payload = sw.missions_json(sw.order_entries(rows))["missions"]
    assert {m["name"]: m["station_idle"] for m in payload}["Finished with station"] is True


def test_finished_mission_with_station_shows_muted_station_idle() -> None:
    picker = sw.MissionPicker([entry("a", "Done", "COMPLETED", station=True)])
    picker.lines(24, 100)
    detail = picker.segment_lines(24, 100)[-2][0]
    assert detail[-1] == ("station idle", "muted")
    assert [text for text, _ in picker.segment_lines(24, 100)[3][0]].count("●") == 0  # not marked live


def test_stale_running_mission_shows_the_stale_marker_like_the_home_screen() -> None:
    picker = sw.MissionPicker([entry("c", "Stale", "RUNNING"), entry("b", "Fine", controller=True)])
    lines = [text for text, _ in picker.lines(24, 100)]
    stale_row = next(line for line in lines if "Stale" in line and "cccccccc" in line)
    assert "● RUNNING   stale" in stale_row
    assert "stale" not in next(line for line in lines if "bbbbbbbb" in line)
    picker.key("KEY_DOWN")  # the selected row is drawn as one bar; look at the stale row unselected
    stale_segments = next(line for line, _s in picker.segment_lines(24, 100) if any("cccccccc" in text for text, _r in line))
    assert (" stale", "warn") in stale_segments and ("● RUNNING  ", "warn") in stale_segments
    only_stale = sw.MissionPicker([entry("c", "Stale", "RUNNING")])
    assert "controller not running" in only_stale.lines(24, 100)[-2][0]
    assert "stale" not in "\n".join(t for t, _ in sw.MissionPicker([entry("b", "Fine", controller=True)]).lines(24, 100))


def test_x_asks_then_closes_the_idle_station_after_y() -> None:
    closed: list[str] = []

    def closer(item: sw.MissionEntry) -> str:
        closed.append(item.mission_id)
        return "closed"

    picker = sw.MissionPicker([entry("a", "Done", "COMPLETED", station=True)], closer=closer)
    assert "x close station" in picker.lines(24, 100)[-1][0]
    assert picker.key("x") is None and picker.confirming is not None
    lines = [text for text, _ in picker.lines(24, 100)]
    assert lines[-2] == "Close the idle station of Done? Y close · N keep" and lines[-1] == "Y close station · N keep it"
    picker.key("n")
    assert picker.confirming is None and closed == [] and picker.entries[0].station
    picker.key("x")
    picker.key("\x1b")  # Esc only declines; it does not leave the picker
    assert picker.confirming is None and not picker.cancelled and closed == []
    picker.key("x")
    picker.key("Y")
    assert closed == ["a" * 32] and not picker.entries[0].station
    last = picker.segment_lines(24, 100)[-2][0]
    assert last == [("Station closed: Done", "ok")]
    assert "x close station" not in picker.lines(24, 100)[-1][0]
    picker.key("KEY_DOWN")
    assert picker.notice == ""


def test_x_keeps_the_station_when_a_provider_process_is_alive() -> None:
    picker = sw.MissionPicker([entry("a", "Done", "COMPLETED", station=True)], closer=lambda _e: "provider-alive")
    picker.key("x")
    picker.key("y")
    assert picker.entries[0].station
    assert picker.segment_lines(24, 100)[-2][0][0][1] == "warn"
    assert "provider process is still running" in picker.notice
    picker.key("x")
    picker.key("y")  # an unknown result code is reported, never treated as closed
    picker.closer = lambda _e: "weird"
    picker.key("x")
    picker.key("y")
    assert "Station kept (weird)" in picker.notice and picker.entries[0].station


def test_x_is_text_for_the_filter_unless_the_selection_has_an_idle_station() -> None:
    picker = sw.MissionPicker([entry("a", "Done", "COMPLETED", station=True), entry("b", "xylophone", controller=True)])
    picker.key("x")
    assert picker.confirming is not None
    picker.key("n")
    picker.key("KEY_DOWN")  # now on a mission without an idle station
    picker.key("x")
    assert picker.query == "x" and picker.confirming is None
    typing = sw.MissionPicker([entry("a", "Done", "COMPLETED", station=True)])
    typing.query = "do"
    typing.key("x")
    assert typing.confirming is None and typing.query == "dox"
    typing.query = "do"
    typing.key("\x18")  # Ctrl-X closes even while a filter is typed
    assert typing.confirming is not None


def test_close_station_uses_the_safe_runtime_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bridge_runtime, "gc_finished_station", lambda mission_id: calls.append(mission_id) or "clients-attached")
    assert sw.close_station(entry("a", status="COMPLETED", station=True)) == "clients-attached"
    assert calls == ["a" * 32]
    monkeypatch.setattr(bridge_runtime, "gc_finished_station", lambda _m: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sw.close_station(entry("a")) == "error: boom"


def test_gc_closes_only_finished_stations_and_reports_json(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    with BridgeStore(state / "bridge.sqlite3") as store:
        done = str(store.create_mission("Done mission", "o", cwd=tmp_path, initial_prompt="o")["id"])
        kept = str(store.create_mission("Busy mission", "o", cwd=tmp_path, initial_prompt="o")["id"])
        live = str(store.create_mission("Live mission", "o", cwd=tmp_path, initial_prompt="o")["id"])
        store.set_mission_status(live, "RUNNING")
        for mission_id in (done, kept):
            store.db.execute("UPDATE missions SET status='COMPLETED' WHERE id=?", (mission_id,))
        store.db.commit()
    original = sw.collect_entries
    monkeypatch.setattr(sw, "collect_entries", lambda store: original(store, station_probe=lambda _m: True, controller_probe=lambda _m: False))
    results = {done: "closed", kept: "provider-alive"}
    seen: list[str] = []
    monkeypatch.setattr(bridge_runtime, "gc_finished_station", lambda mission_id: seen.append(mission_id) or results[mission_id])
    assert bridge.main(["gc"]) == 0
    out = capsys.readouterr().out
    assert sorted(seen) == sorted([done, kept]) and live not in seen
    assert "Station closed  Done mission" in out and "provider process is still running; station kept  Busy mission" in out
    assert out.strip().endswith("1 of 2 stations closed")
    seen.clear()
    assert bridge.main(["gc", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["closed"] == 1 and {s["id"]: s["result"] for s in payload["stations"]} == results
    assert sw.format_gc([]) == "No idle stations of finished missions."
    assert sw.format_gc([{"id": "a" * 32, "name": "N", "result": "closed"}]).endswith("1 of 1 station closed")


# --- 4: picker header alignment ---------------------------------------------------------------------

@pytest.mark.parametrize("width", [60, 80, 120])
@pytest.mark.parametrize("stale", [False, True])
def test_picker_header_is_aligned_with_the_columns(width: int, stale: bool) -> None:
    rows = [
        entry("c", "Alpha work", cwd="/work/alpha", controller=not stale, provider="claude", task_id=3, task_ordinal=2, task_title="Write docs"),
        entry("d", "Beta", "COMPLETED", cwd="/work/beta"),
    ]
    picker = sw.MissionPicker(rows, home="/work")
    lines = [text for text, _ in picker.lines(24, width)]
    header = lines[2]
    assert header.split()[:3] == ["ID", "Status", "Name"]
    body = lines[3]
    assert header.index("ID") == body.index("cccccccc")
    assert header.index("Status") == body.index("● RUNNING")
    assert header.index("Name") == body.index("Alpha")
    if width >= 76:
        assert header.index("Provider") == body.index("claude")
    assert picker.segment_lines(24, width)[2][0][0][1] == "muted"
    plain = picker.plain_text(width).splitlines()
    assert plain[0].index("Name") == plain[1].index("Alpha")
    if "Folder" in header:
        assert header.index("Folder") == body.index("~/alpha")


# --- 5: home form --------------------------------------------------------------------------------

def form(tmp_path: Path, **kw: Any) -> Launcher:
    kw.setdefault("providers", "claude")
    return Launcher(str(tmp_path), creating=True, objective="Do it", installed=("codex", "claude", "copilot"), **kw)


def test_tab_into_providers_puts_the_cursor_on_the_first_selected_entry(tmp_path: Path) -> None:
    state = form(tmp_path, providers="copilot,claude", provider_cursor=2)
    state.key("\t")
    assert state.focus == 1 and state.provider_cursor == 0
    assert state.choices()[state.provider_cursor][0] == "copilot"
    none = form(tmp_path, providers="", provider_cursor=2)
    none.key("\t")
    assert none.choices()[none.provider_cursor][0] == "claude"  # alphabetical when nothing is selected
    state.provider_cursor = 2
    state.focus = 0
    state.key(curses.KEY_DOWN)
    assert state.provider_cursor == 0


@pytest.mark.parametrize("routes", [(), (launcher.RouteChoice("fast", "codex"),)])
def test_shift_tab_into_providers_lands_on_the_first_item(tmp_path: Path, routes: tuple[launcher.RouteChoice, ...]) -> None:
    state = form(tmp_path, routes=routes, focus=2, provider_cursor=2)
    state.key(curses.KEY_BTAB)
    assert state.focus == 1 and state.provider_cursor == 0
    state.provider_cursor = 2
    state.key(curses.KEY_BTAB)  # leaving upwards
    assert state.focus == 0
    state.key("\t")
    assert state.provider_cursor == 0


def test_arrows_stay_in_a_checklist_with_focus_and_tab_leaves_it(tmp_path: Path) -> None:
    state = form(tmp_path, routes=(launcher.RouteChoice("fast", "codex"),), focus=1)
    count = len(state.choices())
    for _ in range(count + 3):
        state.key(curses.KEY_DOWN)
    assert state.focus == 1 and state.provider_cursor == count - 1
    for _ in range(count + 3):
        state.key(curses.KEY_UP)
    assert state.focus == 1 and state.provider_cursor == 0
    state.key("\t")
    assert state.focus == 2
    horizontal = form(tmp_path, focus=1)
    horizontal.vertical = False
    horizontal.key(curses.KEY_DOWN)  # a one-line picker is left with Up/Down
    assert horizontal.focus == 2


def test_folder_is_stripped_and_missing_folders_get_a_plain_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "provider_installed", lambda name: True)
    real = tmp_path / "work dir"
    real.mkdir()
    state = form(tmp_path, directory=f"  {real}  \n")
    state.validate()
    assert state.directory == str(real.resolve())
    monkeypatch.setenv("HOME", str(tmp_path))
    state = form(tmp_path, directory="~/work dir ")
    state.validate()
    assert state.directory == str(real.resolve())
    missing = form(tmp_path, directory=str(tmp_path / "nowhere"), focus=4)
    with pytest.raises(ValueError, match=r"^Folder does not exist: ~/nowhere$"):
        missing.validate()
    missing.key("\n")
    assert missing.error == "Folder does not exist: ~/nowhere" and "Errno" not in missing.error
    afile = tmp_path / "file.txt"
    afile.write_text("x")
    with pytest.raises(ValueError, match=r"^Not a folder: ~/file.txt$"):
        form(tmp_path, directory=str(afile)).validate()


def test_folder_field_shows_the_home_relative_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "dev" / "app"
    work.mkdir(parents=True)
    state = Launcher(str(work), creating=True, installed=("codex",), providers="codex")
    grid = Grid(size=(34, 120))
    launcher._draw(grid, state)
    assert any(row.strip() == "Folder: ~/dev/app" for row in grid.text().splitlines())
    assert str(tmp_path) not in grid.text()
    seen = {}
    monkeypatch.setattr(launcher, "run_launcher", lambda dispatch: seen.setdefault("ran", True) and 0)
    state.directory = launcher.short_path(str(work), 4096)
    assert state.directory == "~/dev/app"
    state.objective = "x"
    state.validate()
    assert state.directory == str(work.resolve())
    assert os.path.isabs(state.directory)
