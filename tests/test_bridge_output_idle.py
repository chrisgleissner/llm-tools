"""Provider output inactivity indicator: unit tests with fakes plus one real scratch tmux server."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

import pytest

from llm_tools import bridge_tui
from llm_tools.terminal import TerminalHandle, TmuxTerminalBackend
from tests.conftest import wait_for as _shared_wait_for


def _wait(predicate, timeout: float = 3.0) -> bool:
    return bool(_shared_wait_for(predicate, timeout, 0.01))


def _running_snapshot(idle: int | None = None, status: str = "RUNNING") -> dict[str, object]:
    snapshot: dict[str, object] = {
        "mission": {"id": "M", "status": "RUNNING"},
        "current_task": {"id": "1", "title": "t", "status": status},
        "tasks": [{"id": "1", "title": "t", "status": status}],
    }
    if idle is not None:
        snapshot["output_idle_seconds"] = idle
    return snapshot


def test_idle_phrase_threshold_and_rounding() -> None:
    assert bridge_tui._idle_phrase({}) == ""
    assert bridge_tui._idle_phrase({"output_idle_seconds": 29}) == ""
    assert bridge_tui._idle_phrase({"output_idle_seconds": "x"}) == ""
    assert bridge_tui._idle_phrase({"output_idle_seconds": 30}) == "no output for 30s"
    assert bridge_tui._idle_phrase({"output_idle_seconds": 59.9}) == "no output for 59s"
    assert bridge_tui._idle_phrase({"output_idle_seconds": 60}) == "no output for 1m"
    assert bridge_tui._idle_phrase({"output_idle_seconds": 119}) == "no output for 1m"
    assert bridge_tui._idle_phrase({"output_idle_seconds": 125}) == "no output for 2m"
    assert bridge_tui._idle_phrase({"output_idle_seconds": 3700}) == "no output for 1h 1m"


def test_status_context_and_now_line_show_observation_only_while_running() -> None:
    context = bridge_tui._station_status_context(_running_snapshot(120))
    assert context == "Task 1 no output for 2m"
    assert len(context) <= 24
    assert "waiting" not in context
    assert bridge_tui._station_status_context(_running_snapshot(5)) == "Task 1 RUNNING"
    assert bridge_tui._station_status_context(_running_snapshot(120, status="VERIFYING")).endswith("VERIFYING")

    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: {}, full=True)
    state.snapshot = _running_snapshot(120)
    now_line = next(row for row in state._bridge_rows() if row.startswith("Now"))
    assert "no output for 2m" in now_line
    state.snapshot = _running_snapshot(3)
    now_line = next(row for row in state._bridge_rows() if row.startswith("Now"))
    assert "no output" not in now_line


def test_observer_shows_indicator_then_clears_when_output_resumes() -> None:
    samples = [(1000, "a"), (1000, "a"), (1100, "b")]
    calls: list[str] = []
    clock = {"now": 1100.0}

    def reader(mission_id: str):
        calls.append(mission_id)
        return samples[min(len(calls), len(samples)) - 1]

    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: _running_snapshot(), full=True)
    state.activity_reader = reader
    state.wall_clock = lambda: clock["now"]
    state.snapshot = _running_snapshot()

    state._observe_output_activity(0.0)
    assert _wait(lambda: state._activity_result is not None)
    state._observe_output_activity(0.5)  # consume first sample; no new poll within 2s
    assert len(calls) == 1
    assert state.snapshot["output_idle_seconds"] == 100

    state._observe_output_activity(2.5)  # second poll starts
    assert _wait(lambda: len(calls) == 2 and state._activity_result is not None)
    state._observe_output_activity(2.6)
    assert state.snapshot["output_idle_seconds"] == 100

    state._observe_output_activity(5.0)  # third poll: provider screen changed at 1100
    assert _wait(lambda: len(calls) == 3 and state._activity_result is not None)
    state._observe_output_activity(5.1)
    assert state.snapshot["output_idle_seconds"] == 0
    assert bridge_tui._idle_phrase(state.snapshot) == ""


def test_observer_ignores_window_activity_without_provider_screen_change() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: {}, full=True)
    state.wall_clock = lambda: 2000.0
    state.snapshot = _running_snapshot()
    state.activity_reader = lambda _m: (1000, "same")
    state._observe_output_activity(0.0)
    assert _wait(lambda: state._activity_result is not None)
    state._observe_output_activity(0.1)
    assert state.snapshot["output_idle_seconds"] == 1000
    state.activity_reader = lambda _m: (1990, "same")  # Bridge pane redraw only
    state._observe_output_activity(10.0)
    assert _wait(lambda: state._activity_result is not None)
    state._observe_output_activity(10.1)
    assert state.snapshot["output_idle_seconds"] == 1000


def test_observer_degrades_silently_on_unreadable_or_raising_reader_and_when_not_running() -> None:
    state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: {}, full=True)
    state.wall_clock = lambda: 2000.0
    state.snapshot = _running_snapshot()
    state._activity_epoch = 1000.0
    state._activity_digest = "x"
    state.activity_reader = lambda _m: None
    state._observe_output_activity(0.0)
    assert _wait(lambda: state._activity_result is not None)
    state._observe_output_activity(0.1)
    assert "output_idle_seconds" not in state.snapshot

    def boom(_m: str):
        raise RuntimeError("tmux gone")

    state.activity_reader = boom
    state._observe_output_activity(5.0)
    assert _wait(lambda: state._activity_result is not None)
    state._observe_output_activity(5.1)
    assert "output_idle_seconds" not in state.snapshot
    assert state.snapshot_error == ""

    state._activity_epoch = 1000.0
    state.snapshot = _running_snapshot(status="READY")
    state._observe_output_activity(9.0)
    assert state._activity_epoch is None
    assert "output_idle_seconds" not in state.snapshot


def test_read_provider_activity_without_station_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import llm_tools.bridge as bridge

    monkeypatch.setattr(bridge, "_load_handle", lambda _m: (_ for _ in ()).throw(RuntimeError("no station")))
    assert bridge_tui._read_provider_activity("M") is None


def test_pane_activity_epoch_missing_tmux_or_server_is_none(tmp_path: Path) -> None:
    handle = TerminalHandle(tmp_path / "none.sock", tmp_path / "none.conf", "s", "%0", "%1", 6)
    assert TmuxTerminalBackend().pane_activity_epoch(handle) is None


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_real_scratch_tmux_reports_activity_epoch_of_single_print(tmp_path: Path) -> None:
    backend = TmuxTerminalBackend()
    handle: TerminalHandle | None = None
    try:
        handle = backend.start(
            provider_argv=["/bin/sh", "-c", "echo once; sleep 30"],
            bridge_argv=["/bin/sh", "-c", "exec cat"],
            cwd=tmp_path,
            socket_path=tmp_path / "tmux.sock",
            session_name="llm-bridge-idle-test",
            size=(100, 30),
        )
        assert _wait(lambda: "once" in backend.capture(handle, pane="provider"))
        printed = int(time.time())
        epoch = backend.pane_activity_epoch(handle, pane="provider")
        assert epoch is not None
        assert abs(epoch - printed) <= 5
        first = epoch
        time.sleep(2.2)
        assert backend.pane_activity_epoch(handle, pane="provider") == first
        idle = time.time() - first
        state = bridge_tui._ConsoleState("M", lambda *_: "", lambda *_: {}, full=True)
        state.snapshot = {"output_idle_seconds": idle + 40}
        assert re.fullmatch(r"no output for 4\ds", bridge_tui._idle_phrase(state.snapshot))
    finally:
        if handle is not None:
            backend.stop(handle)
    assert backend.pane_activity_epoch(handle) is None
