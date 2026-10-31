"""Home screen and mission picker draw on change instead of on a tick, and read the list off the main thread."""

from __future__ import annotations

import curses
from datetime import datetime, timedelta, timezone
import os
import socket
import threading

import pytest

from llm_tools import bridge_launcher as launcher, bridge_redraw, bridge_switcher as sw, bridge_tui as tui
from llm_tools.bridge_launcher import Launcher, MissionStats
from llm_tools.bridge_store import BridgeStore

from tests.test_bridge_launcher_home import Grid, mission, iso


# --- BackgroundReload ---------------------------------------------------------

class Probe:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.values[0] if len(self.values) == 1 else self.values.pop(0)


def finish(reload):
    assert reload.wait(5)


def test_reload_reads_once_then_waits_for_the_probe_to_change():
    reads = []
    probe = Probe(1, 1, 1, 2)
    reload = bridge_redraw.BackgroundReload(lambda: reads.append(1) or len(reads), probe)
    assert reload.poll(0.0, 0.0) is None  # starts the first read without waiting for it
    finish(reload)
    assert reload.poll(0.01, 0.0) == (True, 1)
    assert reload.poll(0.1, 0.0) is None and probe.calls == 1  # before the next probe is due: no probe, no read
    assert reload.poll(0.26, 0.0) is None and probe.calls == 2 and len(reads) == 1  # same version
    assert reload.poll(0.52, 0.0) is None and probe.calls == 3
    assert reload.poll(0.8, 0.0) is None and probe.calls == 4  # version 2: read again
    finish(reload)
    assert reload.poll(0.81, 0.0) == (True, 2)


def test_reload_probes_every_quarter_second_while_active_and_every_second_when_idle():
    probe = Probe(7)
    reload = bridge_redraw.BackgroundReload(lambda: "x", probe)
    reload.poll(0.0, 0.0)
    finish(reload)
    assert reload.poll(0.0, 0.0) == (True, "x")
    assert reload.wake_in(0.1) == pytest.approx(0.15)
    reload.poll(0.25, 0.2)
    assert reload.wake_in(0.25) == pytest.approx(0.25)  # active: fast
    reload.poll(30.0, 0.2)  # idle for 29.8 s
    assert reload.wake_in(30.0) == pytest.approx(1.0)  # idle: slow


def test_reload_request_forces_a_read_and_invalidate_drops_the_one_in_flight():
    gate = threading.Event()
    reads = []

    def load():
        reads.append(1)
        if len(reads) == 1:
            gate.wait(5)
        return len(reads)

    reload = bridge_redraw.BackgroundReload(load, Probe(1))
    reload.poll(0.0, 0.0)
    assert reload.running() and reload.wake_in(0.0) == pytest.approx(0.05)
    reload.invalidate()  # the screen was left and a synchronous reload happened meanwhile
    gate.set()
    finish(reload)
    assert reload.poll(0.0, 0.0) is None  # the stale result is not applied...
    finish(reload)
    assert reload.poll(0.01, 0.0) == (True, 2)  # ...a fresh read is
    reload.request()
    assert reload.wake_in(5.0) == 0.0
    reload.poll(5.0, 5.0)
    finish(reload)
    assert reload.poll(5.01, 5.0) == (True, 3)


def test_reload_holds_back_while_a_form_is_open_and_catches_up_after():
    reload = bridge_redraw.BackgroundReload(lambda: "list", Probe(1))
    assert reload.poll(0.0, 0.0, hold=True) is None and not reload.running()
    reload.poll(0.0, 0.0)
    finish(reload)
    assert reload.poll(0.0, 0.0, hold=True) is None  # a finished read is applied only when not held
    assert reload.poll(0.0, 0.0) == (True, "list")


def test_reload_reports_a_failed_read_and_retries_later():
    reads = []

    def load():
        reads.append(1)
        if len(reads) == 1:
            raise OSError("locked")
        return "ok"

    reload = bridge_redraw.BackgroundReload(load, Probe(1))
    reload.poll(0.0, 0.0)
    finish(reload)
    ok, error = reload.poll(0.0, 0.0)
    assert ok is False and "locked" in str(error)
    assert reload.wake_in(0.0) == pytest.approx(1.0)  # slow retry, not a hot loop
    reload.poll(1.0, 1.0)
    finish(reload)
    assert reload.poll(1.0, 1.0) == (True, "ok")


def test_reload_without_a_probe_reads_only_when_asked_and_treats_an_unreadable_probe_as_changed():
    reload = bridge_redraw.BackgroundReload(lambda: 1)
    assert reload.wake_in(0.0) == 0.0
    reload.poll(0.0)
    finish(reload)
    assert reload.poll(0.0) == (True, 1)
    assert reload.wake_in(0.0) is None and reload.poll(100.0) is None
    # a probe that cannot read yet (database not created) is rebased once the first read has finished
    probe = Probe(None, 4)
    reload = bridge_redraw.BackgroundReload(lambda: 2, probe)
    reload.poll(0.0, 0.0)
    finish(reload)
    assert reload.poll(0.0, 0.0) == (True, 2)
    assert reload.poll(0.3, 0.3) is None and not reload.running()
    broken = bridge_redraw.BackgroundReload(lambda: 3, Probe(None))
    broken.poll(0.0, 0.0)
    finish(broken)
    assert broken.poll(0.0, 0.0) == (True, 3)
    assert broken.wake_in(0.0) == pytest.approx(0.0 + 1.0)  # unknown version: slow, but always a re-read
    assert broken.wait(0.0)


def test_reload_wait_returns_at_once_when_nothing_is_running():
    reload = bridge_redraw.BackgroundReload(lambda: 1)
    reload.poll(0.0)
    finish(reload)
    reload.poll(0.0)
    assert reload.wait(0.0)


# --- age text ----------------------------------------------------------------

@pytest.mark.parametrize("elapsed,expected", [(5, 55), (59, 1), (60, 60), (95, 25), (3599, 1), (3600, 3600), (5400, 1800),
                                              (86399, 1), (86400, 86400), (86400 * 400, 86400 * 365 - (86400 * 400) % (86400 * 365))])
def test_age_changes_in_matches_when_relative_age_text_changes(elapsed, expected):
    now = 1_900_000_000.0
    stamp = datetime.fromtimestamp(now - elapsed, timezone.utc).isoformat()
    until = launcher.age_changes_in(stamp, now)
    assert until == pytest.approx(expected + 0.01)
    before = launcher.relative_age(stamp, now + until - 0.02)
    after = launcher.relative_age(stamp, now + until + 0.02)
    assert before == launcher.relative_age(stamp, now) and after != before
    assert launcher.age_changes_in("not a time", now) is None


# --- cached views --------------------------------------------------------------

def test_column_plan_is_computed_once_per_list_and_width(monkeypatch):
    calls = []
    original = launcher.plan_columns
    monkeypatch.setattr(launcher, "plan_columns", lambda *a: calls.append(a) or original(*a))
    missions = [mission(f"M{i}", ident=f"{i:016d}") for i in range(30)]
    state = Launcher("/w", missions=missions, installed=("codex",))
    first = state.columns(120, 0)
    assert state.columns(120, 0) is first and len(calls) == 1
    state.columns(100, 0)
    state.columns(120, 7)
    assert len(calls) == 3
    state.reload(list(missions), {})
    state.columns(120, 0)
    assert len(calls) == 4


def test_filtered_view_is_computed_once_per_list_and_filter(monkeypatch):
    calls = []
    original = launcher.mission_matches
    monkeypatch.setattr(launcher, "mission_matches", lambda m, needle: calls.append(1) or original(m, needle))
    state = Launcher("/w", missions=[mission("Alpha", ident="a" * 16), mission("Beta", ident="b" * 16)], installed=("codex",),
                     filter_text="alp")
    shown = state.view()
    assert [m["name"] for m in shown] == ["Alpha"] and state.view() is shown and len(calls) == 2
    state.filter_text = "bet"
    assert [m["name"] for m in state.view()] == ["Beta"]
    state.reload([mission("Alpha", ident="a" * 16)], {})
    assert state.view() == []


def test_drawing_reports_the_next_age_change_and_the_next_liveness_probe():
    stamp = iso(timedelta(minutes=5, seconds=50))
    state = Launcher("/w", missions=[mission("Done", "COMPLETED", ident="d" * 16), mission("Live", "RUNNING", ident="l" * 16)],
                     installed=("codex",), probe=lambda _id: True, probe_ttl=2.0)
    state.missions[0]["updated_at"] = stamp
    state.missions[1]["updated_at"] = stamp
    window = Grid(size=(34, 120))
    with bridge_redraw.collect() as timed:
        launcher._draw(window, state)
    assert timed.after is not None and 0.0 < timed.after <= 2.02  # the probe interval, not the ten-second age change
    quiet = Launcher("/w", missions=[mission("Done", "COMPLETED", ident="d" * 16)], installed=("codex",))
    quiet.missions[0]["updated_at"] = stamp
    with bridge_redraw.collect() as timed:
        launcher._draw(Grid(size=(34, 120)), quiet)
    assert 9.0 < timed.after < 10.1  # "5m ago" becomes "6m ago" in ten seconds


def test_loading_screen_names_what_it_waits_for():
    state = Launcher("/w", installed=("codex",), loading=True)
    window = Grid(size=(34, 120))
    launcher._draw(window, state)
    assert "Loading missions" in window.text() and "A mission is a goal" not in window.text()
    state.reload([], {})
    launcher._draw(window, state)
    assert "A mission is a goal" in window.text()


# --- the screen loop ------------------------------------------------------------

class LoopGrid(Grid):
    """Grid whose keys may be None (nothing typed before the timeout) or a callable run at that moment."""

    def __init__(self, keys, size=(34, 120)):
        super().__init__(keys, size)
        self.timeouts: list[int] = []

    def timeout(self, value):
        self.timeouts.append(value)

    def get_wch(self):
        key = next(self.keys)
        if callable(key):
            key = key()
        if key is None:
            raise curses.error("no input")
        return key


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(launcher, "detect_installed", lambda: ("codex",))
    monkeypatch.setattr(launcher, "provider_installed", lambda name: name == "codex")
    monkeypatch.setattr(launcher, "_load_config", lambda: {})
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(tui, "initialize_colors", lambda: False)
    monkeypatch.setattr(tui, "sync_window_size", lambda _window: None)
    frames = []
    original = launcher._draw

    def draw(window, state):
        original(window, state)
        frames.append(window.text())

    monkeypatch.setattr(launcher, "_draw", draw)
    return work, frames


def run(window, monkeypatch):
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    return launcher.run_launcher(lambda args: pytest.fail(args))


def test_an_idle_home_screen_draws_once_and_sleeps_between_wakeups(home, monkeypatch):
    work, frames = home
    with BridgeStore(work.parent / "state" / "bridge.sqlite3") as store:
        store.create_mission("Quiet", "objective", cwd=work)
    window = LoopGrid([None] * 40 + ["\x04"])
    assert run(window, monkeypatch) == 0
    assert len(frames) == 1  # forty timeouts, no repaint (the age "just now" changes in 60 s)
    assert "Quiet" in frames[0]
    assert min(window.timeouts) >= 1 and max(window.timeouts) > 30  # not the old 30 ms tick


def test_a_key_redraws_at_once(home, monkeypatch):
    work, frames = home
    window = LoopGrid([None, "n", None, "\x04"])
    assert run(window, monkeypatch) == 0
    assert len(frames) == 2 and "New mission" in frames[1]


def test_a_resize_redraws(home, monkeypatch):
    work, frames = home
    window = LoopGrid([None, lambda: setattr(window, "size", (30, 100)), None, None, "\x04"])
    # the callable returns None: a timeout during which the terminal was resized
    assert run(window, monkeypatch) == 0
    assert len(frames) == 2


def test_the_list_is_read_in_the_background_and_a_slow_read_does_not_hold_up_the_first_frame(home, monkeypatch):
    work, frames = home
    monkeypatch.setattr(launcher, "_FIRST_LOAD_WAIT", 0.0)
    gate, finished = threading.Event(), threading.Event()
    fresh = [mission("Slow arrival", "COMPLETED", ident="s" * 16)]

    def slow(cwd):
        gate.wait(5)
        finished.set()
        return fresh, {}

    monkeypatch.setattr(launcher, "_load_home", slow)
    instances = []
    original = bridge_redraw.BackgroundReload.__init__
    monkeypatch.setattr(bridge_redraw.BackgroundReload, "__init__",
                        lambda self, *a, **k: (original(self, *a, **k), instances.append(self))[0])

    def release():
        gate.set()
        finished.wait(5)
        instances[0].wait(5)

    window = LoopGrid([None, release, None, "\x04"])
    assert run(window, monkeypatch) == 0
    assert "Loading missions" in frames[0] and "Slow arrival" not in frames[0]
    assert "Slow arrival" in frames[-1]


def test_a_failed_first_read_is_reported_and_retried(home, monkeypatch):
    work, frames = home
    monkeypatch.setattr(launcher, "_FIRST_LOAD_WAIT", 5.0)
    monkeypatch.setattr(launcher, "_load_home", lambda cwd: (_ for _ in ()).throw(OSError("disk on fire")))
    window = LoopGrid(["\x04"])
    assert run(window, monkeypatch) == 0
    assert "Could not read the missions" in frames[-1] and "disk on fire" in frames[-1]


def test_a_commit_by_another_process_reloads_the_list(home, monkeypatch):
    work, frames = home
    monkeypatch.setattr(launcher, "_RELOAD_FAST", 0.0)
    monkeypatch.setattr(launcher, "_RELOAD_SLOW", 0.0)
    path = work.parent / "state" / "bridge.sqlite3"
    with BridgeStore(path) as store:
        store.create_mission("First", "objective", cwd=work)
    instances = []
    original = bridge_redraw.BackgroundReload.__init__
    monkeypatch.setattr(bridge_redraw.BackgroundReload, "__init__",
                        lambda self, *a, **k: (original(self, *a, **k), instances.append(self))[0])

    def other_process_commits():
        with BridgeStore(path) as store:
            store.create_mission("Second", "objective", cwd=work)

    def settle():
        instances[0].wait(5)

    window = LoopGrid([None, other_process_commits, settle, settle, settle, "\x04"])
    assert run(window, monkeypatch) == 0
    assert "First" in frames[0] and "Second" not in frames[0]
    assert "Second" in frames[-1]


def test_an_open_form_is_not_replaced_by_a_reload(home, monkeypatch):
    work, frames = home
    window = LoopGrid(["n", None, None, "\x04"])
    assert run(window, monkeypatch) == 0
    assert all("New mission" in frame for frame in frames[1:])


def test_a_lone_escape_waits_for_its_short_deadline_not_a_tick(home, monkeypatch):
    work, frames = home
    window = LoopGrid(["n", "\x1b", None, None, "\x04"])
    # Esc arms the escape timer; the timeouts that follow run well past 60 ms of real time only in production,
    # so shorten the deadline to zero and see the form close.
    monkeypatch.setattr(launcher, "_ESCAPE_SECONDS", 0.0)
    assert run(window, monkeypatch) == 0
    assert "New mission" in frames[1] and "New mission" not in frames[-1].splitlines()[3]
    assert window.timeouts[window.timeouts.index(min(window.timeouts))] >= 1


def test_a_stalled_paste_is_quarantined_without_a_tick(home, monkeypatch):
    work, frames = home
    monkeypatch.setattr(launcher, "_PASTE_STALL_SECONDS", 0.0)
    window = LoopGrid(["n", "\x1b", "[200~", "a", None, None, "\x04"])
    assert run(window, monkeypatch) == 0
    assert any("Incomplete paste" in frame for frame in frames)


# --- picker ----------------------------------------------------------------------

class PickerWindow:
    def __init__(self, keys, size=(24, 100)):
        self.keys = iter(keys)
        self.size = size
        self.timeouts: list[int] = []
        self.refreshes = 0

    def keypad(self, _value): ...
    def timeout(self, value): self.timeouts.append(value)
    def getmaxyx(self): return self.size
    def erase(self): ...
    def refresh(self): self.refreshes += 1
    def move(self, *_a): ...
    def addnstr(self, *_a): ...
    def noutrefresh(self): ...

    def get_wch(self):
        key = next(self.keys)
        if callable(key):
            key = key()
        if key is None:
            raise curses.error("no input")
        return key


def entries(count=30):
    return [sw.MissionEntry(f"{i:016x}", f"Mission {i}", "RUNNING" if i % 2 else "STOPPED", "/work", provider="claude")
            for i in range(count)]


@pytest.fixture()
def picker_env(monkeypatch):
    monkeypatch.setattr(curses, "curs_set", lambda *_: None)
    monkeypatch.setattr(curses, "mousemask", lambda *_: None)
    monkeypatch.setattr(tui, "initialize_colors", lambda: False)
    monkeypatch.setattr(tui, "sync_window_size", lambda _window: None)


def test_the_idle_picker_draws_once_and_redraws_on_keys_and_resizes(picker_env, monkeypatch):
    window = PickerWindow([None] * 30 + ["j", None, lambda: setattr(window, "size", (30, 90)), None, "\x1b"])
    monkeypatch.setattr(curses, "wrapper", lambda callback: callback(window))
    assert sw.run_picker(entries()) is None
    assert window.refreshes == 3  # first frame, the key, the resize
    assert max(window.timeouts) >= 900  # sleeps until its one-second size check, not a 250 ms tick


def test_search_text_is_built_once_per_entry():
    entry = entries(1)[0]
    first = entry.search_text()
    assert entry.search_text() is first and "mission 0" in first
    assert sw.replace(entry, name="Other").search_text() != first


# --- station liveness ---------------------------------------------------------------

def test_socket_listening_distinguishes_live_dead_and_missing_sockets(tmp_path):
    path = tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    try:
        assert sw.socket_listening(path) is True
    finally:
        server.close()
    assert sw.socket_listening(path) is False  # the file is left behind and refuses
    assert sw.socket_listening(tmp_path / "missing.sock") is False
    plain = tmp_path / "plain"
    plain.write_text("x")
    assert sw.socket_listening(plain) is False


def test_socket_listening_gives_up_on_a_server_that_never_answers(tmp_path, monkeypatch):
    path = tmp_path / "s.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(0)
    fillers = []
    try:
        for _ in range(4):  # fill the backlog so a further connect blocks
            filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            filler.setblocking(False)
            try:
                filler.connect(str(path))
            except OSError:
                pass
            fillers.append(filler)
        assert sw.socket_listening(path, timeout=0.05) is False
    finally:
        for filler in fillers:
            filler.close()
        server.close()


def test_station_probe_connects_in_process_and_never_starts_tmux(tmp_path, monkeypatch):
    path = tmp_path / "m.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    import llm_tools.bridge_runtime as runtime
    import subprocess

    monkeypatch.setattr(runtime, "mission_socket_path", lambda mission_id: path)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a tmux client was started"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a tmux client was started"))
    try:
        assert sw.default_station_probe("m") is True
    finally:
        server.close()
    assert sw.default_station_probe("m") is False
    monkeypatch.setattr(runtime, "mission_socket_path", lambda mission_id: (_ for _ in ()).throw(OSError("no runtime dir")))
    assert sw.default_station_probe("m") is False
