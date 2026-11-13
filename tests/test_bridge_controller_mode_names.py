"""Older controllers stored Background mode as ``actual_mode: "autonomous"``; readers see "background", the row stays as written."""

from __future__ import annotations

import json
from pathlib import Path

from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_store import BridgeStore
from llm_tools.bridge_tui import _humanize_event

OLD = json.dumps({"verified": True, "actual_mode": "autonomous"})


def _store_with_old_event(tmp_path: Path) -> tuple[BridgeStore, str]:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("old mode", "objective", cwd=tmp_path)
    with store.db:
        store.db.execute(
            "INSERT INTO events(mission_id,kind,data_json,created_at) VALUES(?,?,?,?)",
            (mission["id"], "controller-mode", OLD, "2026-01-01T00:00:00+00:00"),
        )
    return store, mission["id"]


def test_an_old_autonomous_event_reads_as_background_through_every_reader(tmp_path: Path) -> None:
    store, mission_id = _store_with_old_event(tmp_path)

    event = next(e for e in store.events(mission_id) if e["kind"] == "controller-mode")
    assert event["data"] == {"verified": True, "actual_mode": "background"}
    latest = store.latest_event(mission_id, "controller-mode")
    assert latest is not None and latest["data"]["actual_mode"] == "background"
    assert _humanize_event(event)[0] == "Controller mode: background"
    assert project_snapshot(store, mission_id)["station_mode"]["actual_mode"] == "background"


def test_the_stored_row_and_the_replicated_payload_keep_the_old_word(tmp_path: Path) -> None:
    store, mission_id = _store_with_old_event(tmp_path)
    store.events(mission_id)
    store.latest_event(mission_id, "controller-mode")
    project_snapshot(store, mission_id)

    assert store.db.execute("SELECT data_json FROM events WHERE kind='controller-mode'").fetchone()[0] == OLD
    replicated = [e for e in store.events_since(0, limit=100) if e["kind"] == "controller-mode"]
    assert replicated[0]["data"]["actual_mode"] == "autonomous"


def test_other_modes_and_other_kinds_are_not_touched(tmp_path: Path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("modes", "objective", cwd=tmp_path)
    store.record_runtime_event(mission["id"], "controller-mode", {"verified": True, "actual_mode": "headless"})
    store.record_runtime_event(mission["id"], "controller-config", {"actual_mode": "autonomous"})
    by_kind = {e["kind"]: e["data"] for e in store.events(mission["id"]) if e["kind"] in {"controller-mode", "controller-config"}}
    assert by_kind["controller-mode"]["actual_mode"] == "headless"
    assert by_kind["controller-config"] == {"actual_mode": "autonomous"}
