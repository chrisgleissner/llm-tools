"""Operation UUIDs must not decide which same-second attempt is newest."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_tools import bridge_store
from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_store import BridgeStore


@pytest.mark.parametrize("second_station", ["codex", "claude"])
def test_latest_operation_follows_insertion_order_with_same_second_timestamps(tmp_path: Path, monkeypatch, second_station: str) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("chronology", "objective", cwd=tmp_path)
        mid = mission["id"]
        store.set_mission_status(mid, "RUNNING")
        first = store.add_task(mid, "first")
        second = store.add_task(mid, "second")
        ids = iter(["f" * 32, "0" * 32])
        monkeypatch.setattr(bridge_store.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
        monkeypatch.setattr(bridge_store, "utc_now", lambda: "2026-10-27T12:00:00+00:00")
        older = store.begin_operation(first, iteration=1, station="claude")
        store.mark_operation_running(older, model="old-model")
        store.record_operation_session(older, provider="claude", restoration_state="UNAVAILABLE")
        store.finish_operation(older, exit_code=0)
        store.verify_task(first, approved=True)
        newer = store.begin_operation(second, iteration=2, station=second_station)
        store.mark_operation_running(newer, model="new-model")
        store.record_operation_session(newer, provider=second_station, restoration_state="RECONSTRUCTED")
        store.finish_operation(newer, exit_code=0)

        snapshot = project_snapshot(store, mid)

        assert snapshot["execution"]["iteration"] == 2
        assert snapshot["station_context"]["last_launched_station"] == second_station
        assert snapshot["operations"][-1]["id"] == newer
        assert snapshot["operations"][-1]["configured_model"] == "new-model"
        station = next(item for item in snapshot["stations"] if item["id"] == second_station)
        assert station["restoration_state"] == "RECONSTRUCTED"
