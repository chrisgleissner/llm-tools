"""The station status line changes in coarse steps so tmux repaints every attached client rarely."""

from __future__ import annotations

from llm_tools import bridge_tui


def _snapshot(idle) -> dict:
    return {
        "mission": {"status": "RUNNING", "name": "demo", "cwd": "/work/demo"},
        "current_task": {"ordinal": 1, "id": 1, "status": "RUNNING"},
        "output_idle_seconds": idle,
    }


def test_station_idle_phrase_steps_by_ten_seconds_then_by_minute() -> None:
    phrase = bridge_tui._station_idle_phrase
    assert phrase(_snapshot(29)) == ""
    assert [phrase(_snapshot(value)) for value in (30, 39, 40, 49.9, 50, 59.9)] == [
        "no output for 30s", "no output for 30s", "no output for 40s", "no output for 40s", "no output for 50s", "no output for 50s",
    ]
    assert phrase(_snapshot(60)) == "no output for 1m"
    assert phrase(_snapshot(119)) == "no output for 1m"
    assert phrase(_snapshot(125)) == "no output for 2m"
    assert phrase(_snapshot(True)) == ""  # a bool is not a duration


def test_the_console_itself_still_counts_idle_seconds() -> None:
    assert bridge_tui._idle_phrase(_snapshot(37)) == "no output for 37s"


def test_status_text_is_identical_for_every_second_in_a_ten_second_step() -> None:
    texts = {bridge_tui._station_status_context(_snapshot(idle)) for idle in range(40, 50)}
    assert len(texts) == 1
    segments = {tuple(bridge_tui._station_status_segments(_snapshot(idle))) for idle in range(40, 50)}
    assert len(segments) == 1
    assert len({bridge_tui._station_status_context(_snapshot(idle)) for idle in range(30, 60)}) == 3
