"""Heartbeat and capacity-refresh retention, and unchanged-observation handling in the refresh worker."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_tools import bridge_monitor
from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_refresh import CapacityRefreshWorker, observation_signature
from llm_tools.bridge_store import BridgeStore

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
PLUMBING = {"controller-heartbeat", "capacity-refresh"}


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _seed(store: BridgeStore, mission_id: str, *, hours: int = 24) -> None:
    """``hours`` of 5 s heartbeats and 60 s capacity refreshes, plus operator and error events spread over the same time."""
    rows = []
    start = NOW - timedelta(hours=hours)
    for second in range(0, hours * 3600, 5):
        moment = start + timedelta(seconds=second)
        rows.append((mission_id, None, None, "controller-heartbeat", json.dumps({"observed_at": _stamp(moment)}), _stamp(moment)))
        if second % 60 == 0:
            rows.append((mission_id, None, None, "capacity-refresh", json.dumps({"observed_at": _stamp(moment), "n": second}), _stamp(moment)))
        if second % 1800 == 0:
            for kind in ("task-added", "operation-prepared", "controller-heartbeat-error", "capacity-refresh-error", "provider-wait", "task-verified"):
                rows.append((mission_id, None, None, kind, "{}", _stamp(moment)))
    with store._transaction():
        store.db.executemany("INSERT INTO events(mission_id,task_id,operation_id,kind,data_json,created_at) VALUES(?,?,?,?,?,?)", rows)


def _counts(store: BridgeStore, mission_id: str) -> dict[str, int]:
    return {row[0]: row[1] for row in store.db.execute("SELECT kind,COUNT(*) FROM events WHERE mission_id=? GROUP BY kind", (mission_id,))}


@pytest.fixture
def seeded(tmp_path: Path):
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        other = store.create_mission("n", "o", cwd=other_dir)
        _seed(store, mission["id"])
        _seed(store, other["id"], hours=2)
        yield store, mission["id"], other["id"]


def test_pruning_bounds_heartbeats_and_thins_old_capacity_refreshes(seeded) -> None:
    store, mid, other = seeded
    before = _counts(store, mid)
    other_before = _counts(store, other)
    newest_heartbeat = store.latest_event(mid, "controller-heartbeat")["id"]
    newest_refresh = store.latest_event(mid, "capacity-refresh")["id"]
    result = store.prune_plumbing_events(mid, now=NOW, budget_seconds=60)
    after = _counts(store, mid)
    assert result["complete"] is True
    # Heartbeats: only the last hour (one every 5 s) remains.
    assert after["controller-heartbeat"] == 3600 // 5
    # Capacity refreshes: every one of the last hour, then one per 10-minute bucket.
    assert after["capacity-refresh"] == 60 + 23 * 6
    # The newest row of each kind survives, so the readers of "latest" keep working.
    assert store.latest_event(mid, "controller-heartbeat")["id"] == newest_heartbeat
    assert store.latest_event(mid, "capacity-refresh")["id"] == newest_refresh
    # Operator and error events are never pruned, and other missions are untouched.
    for kind, count in before.items():
        if kind not in PLUMBING:
            assert after[kind] == count, kind
    assert _counts(store, other) == other_before
    assert result["deleted"]["controller-heartbeat"] == before["controller-heartbeat"] - after["controller-heartbeat"]
    # A second pass has nothing left to delete.
    again = store.prune_plumbing_events(mid, now=NOW, budget_seconds=60)
    assert again["deleted"] == {} and _counts(store, mid) == after


def test_the_newest_row_of_a_kind_survives_even_when_it_is_old(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        old = _stamp(NOW - timedelta(days=3))
        for kind in ("controller-heartbeat", "capacity-refresh"):
            for _ in range(3):
                store.db.execute("INSERT INTO events(mission_id,kind,data_json,created_at) VALUES(?,?,'{}',?)", (mission["id"], kind, old))
        store.prune_plumbing_events(mission["id"], now=NOW)
        assert _counts(store, mission["id"])["controller-heartbeat"] == 1
        assert _counts(store, mission["id"])["capacity-refresh"] == 1


def test_a_pass_stops_at_its_budget_and_resumes(seeded) -> None:
    store, mid, _other = seeded
    first = store.prune_plumbing_events(mid, now=NOW, chunk=500, budget_seconds=0)
    assert first["complete"] is False and sum(first["deleted"].values()) >= 500
    passes = 1
    while not store.prune_plumbing_events(mid, now=NOW, chunk=500, budget_seconds=0)["complete"]:
        passes += 1
        assert passes < 200
    split = _counts(store, mid)
    assert passes > 5  # the retained set below is the same however the work was split
    fresh = store.prune_plumbing_events(mid, now=NOW, budget_seconds=60)
    assert fresh["deleted"] == {} and _counts(store, mid) == split


def test_status_readers_still_find_the_latest_events_after_pruning(seeded, monkeypatch: pytest.MonkeyPatch) -> None:
    store, mid, _other = seeded
    monkeypatch.setattr(bridge_monitor.time, "time", lambda: NOW.timestamp())
    before = project_snapshot(store, mid)
    store.prune_plumbing_events(mid, now=NOW, budget_seconds=60)
    after = project_snapshot(store, mid)
    for key in ("capacity", "lifecycle", "waiting", "suspend", "last_meaningful_progress", "operator_events", "stations"):
        assert after[key] == before[key], key
    assert after["lifecycle"]["heartbeat"]["source"] == "last persisted heartbeat"


def test_audit_event_records_a_pass_and_is_kept_out_of_operator_activity(seeded) -> None:
    store, mid, _other = seeded
    result = store.prune_plumbing_events(mid, now=NOW, budget_seconds=60)
    event_id = store.record_events_pruned(mid, result)
    event = store.events(mid, after=event_id - 1)[0]
    assert event["kind"] == "events-pruned"
    assert event["data"]["deleted"] == result["deleted"] and event["data"]["total"] == sum(result["deleted"].values())
    assert event["data"]["heartbeat_before"] == _stamp(NOW - timedelta(hours=1))
    assert "events-pruned" not in {e["kind"] for e in bridge_monitor.latest_operator_events(store, mid)}
    store.prune_plumbing_events(mid, now=NOW + timedelta(days=30), budget_seconds=60)
    assert _counts(store, mid)["events-pruned"] == 1


# ---------------------------------------------------------------- refresh worker

def _worker(tmp_path: Path, mission_id: str) -> CapacityRefreshWorker:
    return CapacityRefreshWorker(mission_id, tmp_path / "bridge.sqlite3", interval_seconds=5, heartbeat_seconds=1, selector=lambda _m: {})


def _observation(remaining: float, observed_at: str, forecast: str = "about 3h left") -> dict:
    return {"provider": "codex", "observed_at": observed_at,
            "decisions": [{"provider": "codex", "windows": [{"name": "5h", "remaining_percent": remaining, "estimated_remaining": forecast}]}]}


def test_unchanged_observation_updates_the_previous_row_and_a_change_appends(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        mid = mission["id"]
        worker = _worker(tmp_path, mid)
        worker._record_refresh(_observation(80, "t1"))
        worker._record_refresh(_observation(80, "t2", forecast="about 2h left"))
        worker._record_refresh(_observation(80, "t3"))
        rows = [e for e in store.events(mid) if e["kind"] == "capacity-refresh"]
        assert len(rows) == 1 and rows[0]["data"]["observed_at"] == "t3"
        worker._record_refresh(_observation(79, "t4"))
        rows = [e for e in store.events(mid) if e["kind"] == "capacity-refresh"]
        assert [e["data"]["observed_at"] for e in rows] == ["t3", "t4"]
        worker._discard_connection()


def test_a_wait_or_selection_after_the_last_observation_forces_a_new_row(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        mid = mission["id"]
        worker = _worker(tmp_path, mid)
        worker._record_refresh(_observation(80, "t1"))
        store.record_runtime_event(mid, "provider-wait", {"target_epoch": 1})
        worker._record_refresh(_observation(80, "t2"))  # supersedes the wait, as before
        kinds = [e["kind"] for e in store.events(mid) if e["kind"] in {"capacity-refresh", "provider-wait"}]
        assert kinds == ["capacity-refresh", "provider-wait", "capacity-refresh"]
        snapshot = project_snapshot(store, mid)
        assert snapshot["capacity"]["wait"] is None
        worker._discard_connection()


def test_a_restarted_worker_continues_the_previous_observation(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        mid = mission["id"]
        first = _worker(tmp_path, mid)
        first._record_refresh(_observation(80, "t1"))
        first._discard_connection()
        second = _worker(tmp_path, mid)
        second._record_refresh(_observation(80, "t2"))
        assert len([e for e in store.events(mid) if e["kind"] == "capacity-refresh"]) == 1
        second._discard_connection()


def test_worker_prune_pass_removes_expired_rows_and_audits_once(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "o", cwd=tmp_path)
        mid = mission["id"]
        old = datetime.now(timezone.utc) - timedelta(hours=5)
        with store._transaction():
            for index in range(300):
                moment = _stamp(old + timedelta(seconds=5 * index))
                store.db.execute("INSERT INTO events(mission_id,kind,data_json,created_at) VALUES(?,'controller-heartbeat','{}',?)", (mid, moment))
        store.record_runtime_event(mid, "controller-heartbeat", {"observed_at": "fresh"})
        worker = _worker(tmp_path, mid)
        assert worker._prune_pass() is True
        counts = _counts(store, mid)
        assert counts["controller-heartbeat"] == 1 and counts["events-pruned"] == 1
        assert store.latest_event(mid, "events-pruned")["data"]["deleted"] == {"controller-heartbeat": 300}
        assert worker._prune_pass() is True
        assert _counts(store, mid)["events-pruned"] == 1
        worker._discard_connection()


def test_observation_signature_ignores_only_time_dependent_fields() -> None:
    assert observation_signature(_observation(80, "a", "x")) == observation_signature(_observation(80, "b", "y"))
    assert observation_signature(_observation(80, "a")) != observation_signature(_observation(70, "a"))
