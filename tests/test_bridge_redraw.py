"""Redraw scheduling: dirty flag, timed deadlines, change probe, snapshot ageing and cell-width fast paths."""

from __future__ import annotations

import random
import sqlite3
import threading
import unicodedata
from pathlib import Path

import pytest

from llm_tools import bridge_input, bridge_redraw, bridge_tui
from tests.conftest import wait_for


# --- planner -----------------------------------------------------------------

def test_planner_draws_first_then_only_on_mark_or_deadline() -> None:
    planner = bridge_redraw.RedrawPlanner()
    assert planner.should_draw(0.0)
    planner.drawn(None)
    assert not planner.should_draw(1000.0)  # static frame: never again until something changes
    planner.mark()
    assert planner.should_draw(1000.0)
    planner.drawn(10.0)
    assert not planner.should_draw(9.99)
    assert planner.should_draw(10.0)


def test_planner_wait_is_the_earliest_of_deadline_and_other_timers() -> None:
    planner = bridge_redraw.RedrawPlanner()
    planner.drawn(3.75)
    assert planner.wait(3.0) == pytest.approx(0.75)
    assert planner.wait(3.0, 0.5, None) == pytest.approx(0.5)
    assert planner.wait(6.0) == 0.0  # overdue
    planner.drawn(None)
    assert planner.wait(3.0) == bridge_redraw.MAX_WAIT_SECONDS
    assert planner.wait(3.0, 50.0) == bridge_redraw.MAX_WAIT_SECONDS  # never sleeps past the size re-check
    planner.mark()
    assert planner.wait(3.0, 0.5) == 0.0


def test_timeout_ms_rounds_up_and_is_positive() -> None:
    assert bridge_redraw.timeout_ms(0.0) == 1
    assert bridge_redraw.timeout_ms(0.1) == 100
    assert bridge_redraw.timeout_ms(0.2501) == 251


def test_probe_interval_backs_off_after_ten_idle_seconds_and_recovers_on_input() -> None:
    assert bridge_redraw.probe_interval(100.0, 99.0) == 0.25
    assert bridge_redraw.probe_interval(110.0, 100.0) == 1.0
    assert bridge_redraw.probe_interval(110.0, 109.9) == 0.25  # input returns it to the fast cadence at once
    assert bridge_redraw.probe_interval(5.0, None) == 0.25


# --- timed elements ---------------------------------------------------------------

def test_collect_records_reports_from_this_thread_only_and_nests() -> None:
    bridge_redraw.note_wall(1.0)  # outside a frame: ignored
    with bridge_redraw.collect() as outer:
        bridge_redraw.note_wall(3.0)
        with bridge_redraw.collect() as inner:
            bridge_redraw.note_wall(0.5)
        assert inner.after == 0.5
        worker = threading.Thread(target=lambda: bridge_redraw.note_wall(0.01))
        worker.start()
        worker.join()
        bridge_redraw.note_wall(2.0)
    assert outer.after == 2.0


@pytest.mark.parametrize(("value", "steps"), [(0, 1), (30, 1), (58, 1), (59, 1), (60, 60), (61, 59), (119, 1), (125, 55)])
def test_age_steps_follow_the_displayed_granularity(value: int, steps: int) -> None:
    timed = bridge_redraw.TimedElements()
    timed.age(value)
    assert timed.age_steps == steps


def test_age_deadline_falls_on_the_snapshot_second_boundary() -> None:
    timed = bridge_redraw.TimedElements()
    timed.age(3)
    # snapshot read at t=100.0; at t=103.2 the next whole second since the read is 104.0
    assert timed.next_deadline(103.2, age_origin=100.0) == pytest.approx(104.0)
    assert timed.next_deadline(103.2) == pytest.approx(104.2)
    assert bridge_redraw.TimedElements().next_deadline(1.0) is None


def test_wall_and_age_reports_combine_to_the_earliest() -> None:
    timed = bridge_redraw.TimedElements()
    timed.age(3)
    timed.wall(0.3)
    assert timed.next_deadline(10.0, age_origin=9.5) == pytest.approx(10.3)
    tiny = bridge_redraw.TimedElements()
    tiny.wall(0.0)
    assert tiny.next_deadline(10.0) == pytest.approx(10.0 + bridge_redraw.MIN_WAIT_SECONDS)


def test_drawn_text_reports_when_it_will_change() -> None:
    with bridge_redraw.collect() as timed:
        assert bridge_tui._age(7) == "7s"
    assert timed.age_steps == 1
    with bridge_redraw.collect() as timed:
        assert bridge_tui._age(150) == "2m"
    assert timed.age_steps == 30
    now = 1_000_000.5
    with bridge_redraw.collect() as timed:
        assert bridge_tui._relative_epoch(1_000_000 - 90, now) == "1m ago"
    assert timed.after == pytest.approx(29.5)  # 90.5 s old: "2m" at 120 s
    assert timed.age_steps is None  # a wall-clock text must not also claim snapshot-phase ageing
    with bridge_redraw.collect() as timed:
        assert bridge_tui._relative_epoch(1_000_000 - 12, now) == "12s ago"
    assert timed.after == pytest.approx(0.5)
    with bridge_redraw.collect() as timed:
        assert bridge_tui._relative_epoch(1_000_000 + 125, now) == "in 2m"
    assert timed.after == pytest.approx(4.51)  # 124.5 s left: "in 1m" below 120 s
    with bridge_redraw.collect() as timed:
        assert bridge_tui._relative_epoch(1_000_000 + 30, now) == "in 0m"
    assert timed.after == pytest.approx(30.51)


def test_spinner_and_expiring_status_message_report_their_next_change() -> None:
    state = bridge_tui._ConsoleState("M", lambda m, c: "", lambda m: {}, True)
    state.command_pending = True
    state.working_since = 0.0
    with bridge_redraw.collect() as timed:
        assert bridge_tui._working_text(state, now=1.0)
    assert timed.after == pytest.approx(0.1) or bridge_tui.bridge_theme.colors_disabled()
    state = bridge_tui._ConsoleState("M", lambda m, c: "", lambda m: {}, True)
    clock = [100.0]
    state.status_clock = lambda: clock[0]
    state.output.append("done")
    with bridge_redraw.collect() as timed:
        assert state.status_message() == "done"
    assert timed.after == pytest.approx(bridge_tui._STATUS_SECONDS + 0.01)
    clock[0] = 105.0
    with bridge_redraw.collect() as timed:
        assert state.status_message() == "done"
    assert timed.after == pytest.approx(bridge_tui._STATUS_SECONDS - 5.0 + 0.01)
    clock[0] = 109.0
    assert state.status_message() is None


# --- change probe ---------------------------------------------------------------------

def test_store_change_probe_sees_commits_from_other_connections_only(tmp_path: Path) -> None:
    path = tmp_path / "bridge.sqlite3"
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t(x)")
    probe = bridge_redraw.StoreChangeProbe(path)
    first = probe()
    assert first is not None
    assert probe() == first and probe() == first  # reading does not change it
    writer.execute("INSERT INTO t VALUES (1)")
    second = probe()
    assert second is not None and second != first
    assert probe() == second
    probe.close()
    assert probe() is not None  # reopens


def test_store_change_probe_reports_none_when_unavailable(tmp_path: Path) -> None:
    probe = bridge_redraw.StoreChangeProbe(tmp_path / "missing.sqlite3")
    assert probe() is None
    path = tmp_path / "garbage.sqlite3"
    path.write_bytes(b"not a database" * 100)
    assert bridge_redraw.StoreChangeProbe(path)() is None


# --- console refresh: skip unchanged, age locally, back off ---------------------------------

def _state_with_probe(snapshots: list[dict[str, object]], reading: list[object]) -> tuple[bridge_tui._ConsoleState, list[int]]:
    reads: list[int] = []

    def snapshot(mission_id: str, **_projection: object) -> dict[str, object]:
        reads.append(len(reads))
        return snapshots[min(len(reads) - 1, len(snapshots) - 1)]

    state = bridge_tui._ConsoleState("M", lambda m, c: "", snapshot, True)
    state.change_probe = lambda: reading[0]
    return state, reads


def _settle(state: bridge_tui._ConsoleState, now: float) -> None:
    """Run a refresh and wait for its background read, then consume the result."""
    state.refresh(now=now, force=True)
    thread = state._snapshot_thread
    if thread is not None:
        thread.join(2)
    state.refresh(now=now)


BASE = {"mission": {"id": "M", "status": "RUNNING"}, "lifecycle": {"controller_running": True, "heartbeat": {"age_seconds": "2", "reported_state": "ACTIVE"}}}


def test_unchanged_database_skips_the_snapshot_read_until_the_full_refresh_deadline() -> None:
    reading: list[object] = [7]
    state, reads = _state_with_probe([BASE], reading)
    _settle(state, 100.0)
    assert reads == [0] and state.consume_dirty()
    for tick in range(1, 19):  # 4.5 s of quarter-second probes
        state.refresh(now=100.0 + tick * 0.25)
    assert len(reads) == 1 and not state.consume_dirty()
    state.refresh(now=105.01)  # periodic read for clocks, lock files and usage
    state._snapshot_thread.join(2)
    state.refresh(now=105.02)
    assert len(reads) == 2


def test_database_change_or_projection_change_triggers_a_read_and_a_repaint() -> None:
    reading: list[object] = [7]
    changed = {**BASE, "mission": {"id": "M", "status": "PAUSED"}}
    state, reads = _state_with_probe([BASE, changed], reading)
    _settle(state, 100.0)
    state.consume_dirty()
    reading[0] = 8
    state.refresh(now=100.3)
    state._snapshot_thread.join(2)
    state.refresh(now=100.31)
    assert len(reads) == 2 and state.snapshot["mission"]["status"] == "PAUSED" and state.consume_dirty()
    state.refresh(now=100.6)
    assert len(reads) == 2  # same reading: no read
    state.choose_view(1)  # different projection parameters: the unchanged database is not enough
    state.refresh(now=100.9)
    state._snapshot_thread.join(2)
    assert len(reads) == 3


def test_identical_snapshot_does_not_mark_dirty_but_a_failed_probe_forces_reads() -> None:
    reading: list[object] = [None]
    state, reads = _state_with_probe([BASE], reading)
    _settle(state, 100.0)
    state.consume_dirty()
    state.refresh(now=100.3)  # probe unavailable: always read
    state._snapshot_thread.join(2)
    state.refresh(now=100.31)
    assert len(reads) == 2
    assert not state.consume_dirty()  # same content, so nothing to repaint


def test_probe_backs_off_when_idle_and_recovers_on_input() -> None:
    reading: list[object] = [1]
    state, reads = _state_with_probe([BASE], reading)
    _settle(state, 100.0)
    assert state.probe_interval(100.5) == 0.25
    assert state.probe_interval(111.0) == 1.0
    state.note_input(111.0)
    assert state.probe_interval(111.1) == 0.25
    assert state.consume_dirty()


def test_ages_advance_locally_and_never_touch_the_unmodified_read() -> None:
    reading: list[object] = [1]
    state, reads = _state_with_probe([BASE], reading)
    _settle(state, 100.0)
    assert state.snapshot["lifecycle"]["heartbeat"]["age_seconds"] == "2"
    state.refresh(now=103.4)
    assert state.snapshot["lifecycle"]["heartbeat"]["age_seconds"] == "5"
    assert BASE["lifecycle"]["heartbeat"]["age_seconds"] == "2"
    assert state.age_origin() == 100.0
    assert len(reads) == 1
    state.snapshot = {"replaced": True}  # a caller replacing the snapshot stops the ageing
    state.refresh(now=110.0)
    assert state.snapshot == {"replaced": True} and state.age_origin() is None


def test_running_operation_elapsed_advances_only_while_it_executes() -> None:
    running = {"station_context": {"executing_operation": "op"}, "execution": {"operation_elapsed_seconds": 10, "seconds_since_verified_progress": 4}}
    idle = {"station_context": {"executing_operation": None}, "execution": {"operation_elapsed_seconds": 10, "seconds_since_verified_progress": 4}}
    assert bridge_tui._aged_snapshot(running, 3)["execution"] == {"operation_elapsed_seconds": 13, "seconds_since_verified_progress": 7}
    assert bridge_tui._aged_snapshot(idle, 3)["execution"] == {"operation_elapsed_seconds": 10, "seconds_since_verified_progress": 7}
    assert bridge_tui._aged_snapshot(running, 0) == running and bridge_tui._aged_snapshot(running, 0) is not running
    assert bridge_tui._aged_snapshot({"usage": {"age_seconds": True}}, 2) == {"usage": {"age_seconds": True}}
    assert running["execution"]["operation_elapsed_seconds"] == 10


def test_wake_in_covers_workers_probe_and_idle_text() -> None:
    reading: list[object] = [1]
    state, _reads = _state_with_probe([BASE], reading)
    _settle(state, 100.0)
    assert state.wake_in(100.0) == pytest.approx(0.25)
    assert state.wake_in(100.2) == pytest.approx(0.05)
    state.command_pending = True
    assert state.wake_in(100.2) == pytest.approx(0.05)  # a worker may publish at any moment
    state.command_pending = False
    state._snapshot_thread = None
    state.snapshot = {"current_task": {"status": "RUNNING"}}
    state.last_refresh = 1e9  # probe not due for a long time
    state.wall_clock = lambda: 1000.0
    state._activity_epoch = 990.0
    state._activity_polled = 100.0
    assert state.wake_in(101.0) == pytest.approx(1.0)  # activity poll due in 1 s
    state._activity_polled = 1e9
    assert state.wake_in(101.0) == pytest.approx(20.0)  # "no output for 30s" appears 20 s from now
    state.wall_clock = lambda: 1020.5
    assert state.wake_in(101.0) == pytest.approx(0.5)  # 30.5 s of quiet: next second tick
    state.wall_clock = lambda: 1090.5
    assert state.wake_in(101.0) == pytest.approx(19.5)  # 100.5 s: the minute count changes at 120 s
    state.snapshot = {}
    assert state.wake_in(101.0) > 1000  # no running task: nothing to watch but the (distant) probe


def test_idle_text_change_marks_dirty_only_when_the_text_changes() -> None:
    state = bridge_tui._ConsoleState("M", lambda m, c: "", lambda m: {}, True)
    state.snapshot = {"current_task": {"status": "RUNNING"}}
    state.wall_clock = lambda: 1000.0
    state._activity_polled = 0.0
    state._activity_epoch = 995.0
    state.consume_dirty()
    state._observe_output_activity(0.5)
    assert not state.consume_dirty()  # 5 s of quiet shows nothing
    state.wall_clock = lambda: 1031.0
    state._observe_output_activity(0.6)
    assert state.consume_dirty() and state.snapshot["output_idle_seconds"] == 36
    state._observe_output_activity(0.7)
    state.consume_dirty()
    state.wall_clock = lambda: 1032.0
    state._observe_output_activity(0.8)
    assert state.consume_dirty()  # "no output for 37s" -> "…38s"


# --- activity polling -------------------------------------------------------------------

def test_activity_is_observed_only_while_a_native_operation_executes() -> None:
    state = bridge_tui._ConsoleState("M", lambda m, c: "", lambda m: {}, True)
    calls: list[str] = []

    def reader(mission_id: str) -> tuple[int, str]:
        calls.append(mission_id)
        return 1000, "d"

    state.activity_reader = reader
    state.snapshot = {"current_task": {"status": "RUNNING"}, "station_context": {"executing_operation": None}}
    state._observe_output_activity(0.0)
    assert calls == [] and state._activity_thread is None  # station described, nothing executing: no polling at all
    state.snapshot = {"current_task": {"status": "RUNNING"}, "station_context": {"executing_operation": "op-1"}}
    state._observe_output_activity(0.0)
    assert wait_for(lambda: state._activity_thread is None, 2)
    assert calls == ["M"]
    state._observe_output_activity(1.0)
    assert len(calls) == 1  # next poll is two seconds out
    state._observe_output_activity(2.5)
    assert wait_for(lambda: state._activity_thread is None, 2)
    assert len(calls) == 2


def test_provider_activity_captures_the_screen_only_when_the_epoch_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import bridge, terminal

    captures: list[int] = []
    epochs = iter([10, 10, 10, 11, None, 11])

    class Backend:
        def pane_activity_screen(self, handle: object, *, pane: str, seen_epoch: int | None = None):
            epoch = next(epochs)
            if epoch is None:
                return None
            if epoch == seen_epoch:  # tmux itself skips the capture when the epoch is unchanged
                return epoch, None
            captures.append(1)
            return epoch, f"screen {len(captures)}"

    monkeypatch.setattr(bridge, "_load_handle", lambda mission_id: object())
    monkeypatch.setattr(terminal, "TmuxTerminalBackend", Backend)
    bridge_tui._ACTIVITY_SEEN.clear()
    first = bridge_tui._read_provider_activity("M")
    assert first is not None and first[0] == 10
    assert bridge_tui._read_provider_activity("M") == first
    assert bridge_tui._read_provider_activity("M") == first
    assert len(captures) == 1
    third = bridge_tui._read_provider_activity("M")
    assert third is not None and third[0] == 11 and third[1] != first[1] and len(captures) == 2
    assert bridge_tui._read_provider_activity("M") is None  # pane gone forgets the screen
    again = bridge_tui._read_provider_activity("M")
    assert again is not None and len(captures) == 3


# --- per-frame row memo and width fast paths ----------------------------------------------------

def test_task_list_rows_are_built_once_per_frame_and_selection_needs_no_rows() -> None:
    tasks = [{"id": f"t{i}", "ordinal": i, "status": "READY", "title": f"Task {i}", "depends_on": [f"t{i - 1}"] if i else []} for i in range(1, 60)]
    state = bridge_tui._ConsoleState("M", lambda m, c: "", lambda m: {}, True)
    state.snapshot = {"tasks": tasks}
    state.choose_view(bridge_tui.VIEWS.index("Tasks"))
    built = []
    original = bridge_tui._order_list_segments
    bridge_tui._order_list_segments = lambda items: built.append(1) or original(items)  # type: ignore[assignment]
    try:
        assert state.selection_rows() == list(range(59)) and built == []
        state._draw_cache = {}
        state.rows(), state.row_segments(), state.rows()
        assert built == [1]
        state._draw_cache = None
        state.rows()
        state.rows()
        assert built == [1, 1, 1]
    finally:
        bridge_tui._order_list_segments = original  # type: ignore[assignment]
    state.snapshot = {"tasks": []}
    assert state.selection_rows() == [0] and state.rows() == ["No tasks recorded."]


def test_waiting_on_uses_a_supplied_status_map() -> None:
    tasks = [{"id": "1", "status": "COMPLETED"}, {"id": "2", "status": "READY"}]
    task = {"depends_on": ["1", "2", "9"]}
    assert bridge_tui._waiting_on(task, tasks) == "after 2, 9"
    assert bridge_tui._waiting_on(task, [], {"1": "COMPLETED", "2": "READY"}) == "after 2, 9"


def _reference_width(text: str) -> int:
    width = 0
    for char in text:
        if char == "\t":
            width += 8 - width % 8
            continue
        if unicodedata.combining(char) or unicodedata.category(char) in {"Cf", "Mn", "Me"}:
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} or ord(char) >= 0x1F300 else 1
    return width


def test_cell_width_fast_paths_match_the_reference_measurement() -> None:
    rng = random.Random(2)
    alphabet = "ab c\t日é\x00\x7f…😀​́~"
    for _ in range(4000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        assert bridge_input.cell_width(text) == _reference_width(text), text
    long_text = "x" * 700 + "日"
    assert bridge_input.cell_width(long_text) == 702
    assert bridge_input.cell_width("plain ascii text") == 16


def test_cell_slice_fast_path_matches_the_measured_slice() -> None:
    rng = random.Random(1)
    for _ in range(4000):
        alphabet = "abc d\t日é\x00" if rng.random() < 0.3 else "abc d"
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        start, width = rng.randint(-2, 14), rng.randint(-2, 14)
        assert bridge_input.cell_slice(text, start, width) == bridge_input._cell_slice_measured(text, start, width)


def test_text_fast_path_matches_the_character_filter() -> None:
    assert bridge_tui._text("plain   words\tok", 50) == "plain words ok"
    assert bridge_tui._text("a\x1b[31mred‮", 50) == "a [31mred"
    assert bridge_tui._text("x" * 20, 5) == "xxxxx"
