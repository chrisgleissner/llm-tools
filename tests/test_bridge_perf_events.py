"""Activity paging starts from remembered id bounds instead of skipping rows from the newest event."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_tools import bridge_monitor
from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_store import BridgeStore


@pytest.fixture(autouse=True)
def _clear_cursors() -> None:
    bridge_monitor._page_cursors.clear()


def _mission_with_events(store: BridgeStore, tmp_path: Path, count: int = 130) -> str:
    mission = store.create_mission("m", "o", cwd=tmp_path)
    with store._transaction():
        for index in range(count):
            store.db.execute(
                "INSERT INTO events(mission_id,kind,data_json,created_at) VALUES(?,?,?,?)",
                (mission["id"], "note", f'{{"n":{index}}}', "now"),
            )
    return mission["id"]


def _direct(store: BridgeStore, mission_id: str, anchor: int, offset: int) -> list[int]:
    return [row[0] for row in store.db.execute(
        "SELECT id FROM events WHERE mission_id=? AND id<=? ORDER BY id DESC LIMIT 20 OFFSET ?", (mission_id, anchor, offset))]


def test_pages_equal_plain_offset_pages_in_every_visiting_order(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mid = _mission_with_events(store, tmp_path)
        anchor = int(store.db.execute("SELECT MAX(id) FROM events").fetchone()[0])
        # New events after the anchor must not move the pages below it.
        store.record_runtime_event(mid, "late", {})
        orders = [[0, 20, 40, 60, 80, 100, 120, 140], [140, 100, 60, 20, 0], [33, 7, 100, 13, 120, 20, 60], [60, 60, 60]]
        for order in orders:
            bridge_monitor._page_cursors.clear()
            for offset in order:
                snapshot = project_snapshot(store, mid, event_offset=offset, event_anchor=anchor)
                assert [event["id"] for event in reversed(snapshot["events"])] == _direct(store, mid, anchor, offset), (order, offset)


def test_sequential_paging_reads_from_a_remembered_bound(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mid = _mission_with_events(store, tmp_path)
        anchor = int(store.db.execute("SELECT MAX(id) FROM events").fetchone()[0])
        for offset in (20, 40, 60, 80):
            project_snapshot(store, mid, event_offset=offset, event_anchor=anchor)
        statements: list[str] = []
        store.db.set_trace_callback(statements.append)
        project_snapshot(store, mid, event_offset=100, event_anchor=anchor)
        project_snapshot(store, mid, event_offset=80, event_anchor=anchor)
        store.db.set_trace_callback(None)
        pages = [s for s in statements if "FROM events WHERE mission_id=" in s and "LIMIT 20 OFFSET" in s]
        assert pages and all(s.endswith("OFFSET 0") for s in pages), pages


def test_moving_newest_page_is_not_cached(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mid = _mission_with_events(store, tmp_path, count=45)
        first = project_snapshot(store, mid)
        store.record_runtime_event(mid, "newer", {})
        second = project_snapshot(store, mid)
        assert second["events"][-1]["kind"] == "newer" and first["events"][-1]["kind"] != "newer"
        assert bridge_monitor._page_cursors == {}


def test_cursor_memory_is_bounded(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mid = _mission_with_events(store, tmp_path, count=30)
        for anchor in range(1, bridge_monitor._PAGE_CURSOR_KEYS + 20):
            project_snapshot(store, mid, event_offset=20, event_anchor=anchor)
        assert len(bridge_monitor._page_cursors) == bridge_monitor._PAGE_CURSOR_KEYS
