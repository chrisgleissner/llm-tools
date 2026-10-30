"""Durable monitor projections stay bounded and do not invent live state."""

from __future__ import annotations

import time

from llm_tools.bridge_monitor import project_snapshot
from llm_tools.bridge_refresh import CapacityRefreshWorker
from llm_tools.bridge_store import BridgeStore
from llm_tools.bridge_tui import _bounded_snapshot, _station_label


def test_projection_paginates_tasks_and_search_without_loading_full_queue(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("large", "objective", cwd=tmp_path)
    for index in range(120):
        store.add_task(mission["id"], f"task body {index}", title=f"Task {index:03}")
    calls = 0
    original = store._task_dicts

    def counted(rows):
        nonlocal calls
        calls += len(rows)
        return original(rows)

    store._task_dicts = counted
    page = project_snapshot(store, mission["id"], task_limit=7, task_offset=21)
    assert [task["title"] for task in page["tasks"]] == [f"Task {index:03}" for index in range(21, 28)]
    assert page["task_total"] == 120
    assert calls == 7
    match = project_snapshot(store, mission["id"], task_limit=3, search="task body 119")
    assert [task["prompt"] for task in match["tasks"]] == ["task body 119"]
    assert match["task_total"] == 1
    store.close()


def test_projection_includes_only_bounded_recent_amendment_history(tmp_path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("amendments", "objective", cwd=tmp_path)
        task_id = store.add_task(mission["id"], "original")
        for revision in range(2, 10):
            store.amend_task(task_id, prompt=f"revision {revision}")

        task = project_snapshot(store, mission["id"], task_limit=1)["tasks"][0]
        assert [entry["revision"] for entry in task["amendments"]] == [9, 8, 7, 6, 5]
        assert task["amendments"][0]["changes"] == {"prompt": "revision 9"}


def test_pending_proposals_are_counted_separately_from_runnable_tasks(tmp_path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = store.create_mission("proposals", "objective", cwd=tmp_path)
        task_id = store.add_task(mission["id"], "operator task")
        store.set_mission_status(mission["id"], "RUNNING")
        operation_id = store.begin_operation(task_id, iteration=1, station="codex")
        store.finish_operation(operation_id, exit_code=0)
        proposed_id = store.propose_task(mission["id"], "agent follow-up", source_operation_id=operation_id)

        snapshot = project_snapshot(store, mission["id"], task_limit=10)
        assert snapshot["task_counts"] == {"VERIFYING": 1}
        assert snapshot["proposal_counts"] == {"PENDING": 1}
        assert next(task for task in snapshot["tasks"] if task["id"] == proposed_id)["proposal_status"] == "PENDING"


def test_projection_uses_persisted_capacity_and_does_not_invent_opaque_values(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("capacity", "objective", cwd=tmp_path, config={
        "headless": True,
        "providers": "kilo,codex",
        "frozen_tool_config": '[providers.kilo]\nmodel = "zai/glm-4.7"\neffort = "high"\n',
    })
    task_id = store.add_task(mission["id"], "work", acceptance=["tests pass", "artifact exists"])
    store.set_mission_status(mission["id"], "RUNNING")
    op = store.begin_operation(task_id, iteration=1, station="kilo-zai")
    store.mark_operation_running(op, process_pid=42, process_start="test-start", route_id="kilo-zai", model="zai/glm-5.2")
    store.record_runtime_event(mission["id"], "provider-selection", {
        "provider": "kilo", "route": "kilo-zai", "selected_model": "zai/glm-5.2",
        "decisions": [{"provider": "zai", "windows": [{"name": "5h", "kind": "reset_window", "remaining_percent": 73, "reset_epoch": int(time.time()) + 600}]}],
    })

    snapshot = project_snapshot(store, mission["id"])
    assert snapshot["current_task"]["id"] == task_id
    assert snapshot["criteria"] == {"verified": 0, "total": 2, "complete": False}
    assert snapshot["station_observation"] == {"station": "unknown", "model": "unknown", "effort": "unknown"}
    assert snapshot["station_context"]["operation_status"] == "RUNNING"
    assert snapshot["station_context"]["provider_liveness"] == "unknown"
    assert snapshot["station_config"]["models"] == {"kilo": "zai/glm-4.7"}
    assert snapshot["station_mode"] == {
        "configured_mode": "headless", "actual_mode": "unknown", "source": "unavailable",
    }
    assert snapshot["capacity"]["selection"]["selected_model"] == "zai/glm-5.2"
    assert snapshot["capacity"]["next_eligible_epoch"] is None
    assert snapshot["capacity"]["source"] == "persisted runtime event"
    assert snapshot["capacity"]["source_age_seconds"] is not None
    assert snapshot["capacity"]["live"] is False
    assert snapshot["lifecycle"]["controller"] == "unknown"
    assert snapshot["suspend"]["status"] == "unknown"
    # Scheduler-pinned model in operations is not an observed provider model.
    assert snapshot["stations"]
    bounded = _bounded_snapshot(snapshot)
    station_rows = bounded["stations"]
    assert any("kilo-zai" in _station_label(row) for row in station_rows)
    assert any(row["observed_model"] == "unknown" for row in station_rows)
    assert bounded["lifecycle"]["status"] == "RUNNING"
    assert bounded["lifecycle"]["controller"] == "unknown"
    assert bounded["station_mode"]["configured_mode"] == "headless"
    assert bounded["station_mode"]["actual_mode"] == "unknown"
    assert snapshot["health"]["max_provider_failures"] == 4
    store.record_runtime_event(mission["id"], "station-observation", {
        "station": "kilo-zai", "observed_model": "zai/glm-5.2", "verified": True,
    })
    verified = project_snapshot(store, mission["id"])
    assert verified["station_observation"]["station"] == "kilo-zai"
    assert verified["station_observation"]["model"] == "zai/glm-5.2"
    store.record_runtime_event(mission["id"], "controller-mode", {
        "verified": True, "actual_mode": "native",
    })
    mode_verified = project_snapshot(store, mission["id"])
    assert mode_verified["station_mode"] == {
        "configured_mode": "headless", "actual_mode": "native", "source": "verified controller-mode event",
    }
    assert _bounded_snapshot(mode_verified)["station_mode"]["actual_mode"] == "native"
    assert snapshot["lifecycle"]["status"] == "RUNNING"
    assert snapshot["last_meaningful_progress"] is None
    store.close()


def test_reopen_projects_durable_progress_and_stall_is_only_repeated_failures(tmp_path) -> None:
    path = tmp_path / "bridge.sqlite3"
    store = BridgeStore(path)
    mission = store.create_mission("restart", "objective", cwd=tmp_path)
    ids = [store.add_task(mission["id"], f"task {i}") for i in range(3)]
    store.set_mission_status(mission["id"], "RUNNING")
    for index, task_id in enumerate(ids, 1):
        op = store.begin_operation(task_id, iteration=index, station="codex")
        store.finish_operation(op, exit_code=1)
    store.close()

    reopened = BridgeStore(path)
    snapshot = project_snapshot(reopened, mission["id"])
    assert snapshot["health"]["consecutive_failed_operations"] == 3
    assert snapshot["health"]["stall_warning"]
    assert snapshot["capacity"]["source"] == "unavailable"
    assert snapshot["capacity"]["source_age_seconds"] is None
    assert snapshot["last_meaningful_progress"] is None
    assert snapshot["persistence"]["journal_mode"] == "wal"
    reopened.close()


def test_latest_capacity_wait_and_progress_survive_more_than_one_hundred_events(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("churn", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "verified work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    store.verify_task(task_id, evidence="tests passed")
    wait_target = int(time.time()) + 600
    store.record_runtime_event(mission["id"], "provider-selection", {
        "provider": "codex", "decision": {"wait_until": wait_target},
        "decisions": [{"provider": "codex", "windows": [{"name": "weekly", "reset_epoch": wait_target + 900}]}],
    })
    store.record_runtime_event(mission["id"], "provider-wait", {"target_epoch": wait_target, "reason": "rate-limited"})
    store.record_runtime_event(mission["id"], "controller-ownership", {
        "status": "RUNNING", "ownership_verified": True, "process_start": "verified-start-token",
    })
    store.record_runtime_event(mission["id"], "suspend_deferred_awake", {
        "target_epoch": wait_target, "reason": "suspend disabled",
    })
    suspend_kinds = (
        "suspend_until_renewal", "suspend_deferred_awake",
        "suspend_unreliable", "suspend_fallback_awake",
    )
    for index in range(80):
        store.record_runtime_event(mission["id"], suspend_kinds[index % len(suspend_kinds)], {
            "target_epoch": wait_target, "sequence": index,
        })
    for index in range(110):
        store.add_task(mission["id"], f"churn {index}")

    snapshot = project_snapshot(store, mission["id"], task_limit=5)
    assert snapshot["capacity"]["next_eligible_epoch"] == wait_target
    assert snapshot["capacity"]["wait"]["target_epoch"] == wait_target
    assert snapshot["last_meaningful_progress"]["kind"] == "task-verified"
    assert len(snapshot["events"]) <= 20
    assert snapshot["lifecycle"]["controller"] == "RUNNING"
    assert snapshot["lifecycle"]["controller_source"] == "verified persisted ownership event"
    assert snapshot["suspend"]["status"] == "suspend_fallback_awake"
    assert snapshot["suspend"]["target_epoch"] == wait_target
    store.close()


def test_new_usable_selection_clears_older_wait_and_uses_readiness_target(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("readiness", "objective", cwd=tmp_path)
    old_wait = int(time.time()) + 600
    selected_wait = old_wait + 300
    store.record_runtime_event(mission["id"], "provider-selection", {
        "provider": "codex", "decision": {"usable": False, "wait_until": selected_wait},
        "decisions": [{"windows": [
            {"name": "5h", "reset_epoch": old_wait},
            {"name": "weekly", "reset_epoch": selected_wait},
        ]}],
    })
    store.record_runtime_event(mission["id"], "provider-wait", {
        "target_epoch": old_wait + 120, "reason": "all-rate-limited",
    })
    waiting = project_snapshot(store, mission["id"])
    assert waiting["capacity"]["next_eligible_epoch"] == old_wait + 120
    assert waiting["capacity"]["wait"]["target_epoch"] == old_wait + 120

    ready_event_id = store.record_runtime_event(mission["id"], "provider-selection", {
        "provider": "codex", "decision": {"usable": True}, "decisions": [],
    })
    ready = project_snapshot(store, mission["id"])
    assert ready["capacity"]["next_eligible_epoch"] is None
    assert ready["capacity"]["wait"] is None
    assert ready["capacity"]["source_event_id"] == ready_event_id
    store.close()


def test_new_capacity_refresh_clears_interleaved_provider_wait(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("refresh-order", "objective", cwd=tmp_path)
    old_target = int(time.time()) + 600
    selected = {
        "provider": "codex",
        "decision": {"usable": False, "wait_until": old_target + 300},
        "decisions": [{"provider": "codex", "usable": False}],
    }
    store.record_runtime_event(mission["id"], "provider-selection", selected)
    wait_id = store.record_runtime_event(mission["id"], "provider-wait", {
        "target_epoch": old_target, "reason": "rate-limited",
    })
    waiting = project_snapshot(store, mission["id"])
    assert waiting["capacity"]["source_event_id"] == wait_id
    assert waiting["capacity"]["next_eligible_epoch"] == old_target

    refresh_id = store.record_runtime_event(mission["id"], "capacity-refresh", {
        "provider": "codex", "decision": {"usable": True}, "decisions": [],
        "observed_at": "2026-10-25T12:00:00+00:00",
    })
    refreshed = project_snapshot(store, mission["id"])
    assert refreshed["capacity"]["source_event_id"] == refresh_id
    assert refreshed["capacity"]["source_event_kind"] == "capacity-refresh"
    assert refreshed["capacity"]["next_eligible_epoch"] is None
    assert refreshed["capacity"]["wait"] is None
    store.close()


def test_refresh_worker_events_project_as_last_observed_not_live(tmp_path) -> None:
    database = tmp_path / "bridge.sqlite3"
    store = BridgeStore(database)
    mission = store.create_mission("worker-contract", "objective", cwd=tmp_path)
    observation = {
        "provider": "codex", "route": "", "selected_model": "gpt-6.1-sol",
        "decision": {"usable": True}, "decisions": [], "observed_at": "2026-10-25T12:00:00+00:00",
    }
    worker = CapacityRefreshWorker(
        mission["id"], database, interval_seconds=5, heartbeat_seconds=1,
        initial_refresh_delay_seconds=0, selector=lambda _mission: observation,
    )
    worker.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        kinds = {event["kind"] for event in store.events(mission["id"])}
        if {"controller-heartbeat", "capacity-refresh"}.issubset(kinds):
            break
        time.sleep(0.01)
    worker.stop()

    events = store.events(mission["id"])
    refresh = next(event for event in events if event["kind"] == "capacity-refresh")
    snapshot = project_snapshot(store, mission["id"])
    heartbeat = snapshot["lifecycle"]["heartbeat"]
    assert snapshot["capacity"]["selection"]["selected_model"] == "gpt-6.1-sol"
    assert snapshot["capacity"]["source_event_id"] == refresh["id"]
    assert heartbeat["reported_state"] == "ACTIVE"
    assert heartbeat["owner_lock_held"] is True
    assert heartbeat["current_liveness"] == "unknown"
    assert isinstance(heartbeat["age_seconds"], int)
    store.close()


def test_latest_event_queries_use_mission_kind_id_index(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("event-index", "objective", cwd=tmp_path)
    suspend_kinds = ("suspend_until_renewal", "suspend_deferred_awake", "suspend_unreliable", "suspend_fallback_awake")
    indexed_kinds = (
        "provider-selection", "capacity-refresh", "provider-wait", "task-verified",
        "controller-heartbeat", "controller-mode", "station-observation",
        "provider-handoff", "controller-ownership", *suspend_kinds,
    )
    indexed_queries = [
        ("SELECT * FROM events INDEXED BY events_mission_kind_id WHERE mission_id=? AND kind=? ORDER BY id DESC LIMIT 1", (mission["id"], kind))
        for kind in indexed_kinds
    ]
    for sql, args in indexed_queries:
        plan = " ".join(str(row[3]) for row in store.db.execute("EXPLAIN QUERY PLAN " + sql, args))
        assert "events_mission_kind_id" in plan
        assert "TEMP B-TREE" not in plan
    store.close()


def test_viewed_station_and_native_history_are_independent_of_executing_station(tmp_path):
    with BridgeStore(tmp_path/'bridge.sqlite3') as store:
        mission = store.create_mission('stations','objective',cwd=tmp_path,config={'providers':'claude,codex','viewed_station':'claude'})
        mid = mission['id']
        store.set_mission_status(mid,'RUNNING')
        first = store.add_task(mid,'first')
        operation = store.begin_operation(first,iteration=1,station='claude')
        store.mark_operation_running(operation,model='sonnet',route_id='claude-old')
        store.record_operation_session(operation,provider='claude',restoration_state='NATIVE_CREATED',session_id='fixture-session',cwd=str(tmp_path),source='claude:system.init')
        store.finish_operation(operation,exit_code=0)
        store.verify_task(first,approved=True)
        second = store.add_task(mid,'second')
        current = store.begin_operation(second,iteration=2,station='codex')
        store.mark_operation_running(current,model='gpt-6-luna')
        snapshot = project_snapshot(store,mid)
        assert snapshot['station_context']['viewed_station'] == 'claude'
        assert snapshot['station_context']['last_launched_station'] == 'codex'
        assert snapshot['station_context']['executing_operation'] == current
        claude = next(row for row in snapshot['stations'] if row['id']=='claude')
        assert claude['restoration_state'] == 'NATIVE_CREATED'
        assert any(row['id']=='claude-old' for row in snapshot['stations'])
        assert snapshot['execution']['iteration'] == 2
        assert snapshot['execution']['operation_elapsed_seconds'] >= 0


def test_convergence_uses_bounded_persisted_evidence_and_verified_progress_resets_warning(tmp_path):
    with BridgeStore(tmp_path/'bridge.sqlite3') as store:
        mission = store.create_mission('convergence','objective',cwd=tmp_path)
        mid = mission['id']
        for station in ['codex','claude','codex']:
            store.record_runtime_event(mid,'operation-progress',{'before':'same','after':'same','station':station,'exit_code':1,'output_sha256':'identical'})
        snapshot = project_snapshot(store,mid)
        assert snapshot['health']['provider_cycles_without_change'] is True
        assert snapshot['health']['identical_failures'] == 3
        assert 'without observed repository change' in snapshot['health']['stall_warning']
        store.record_runtime_event(mid,'task-verified',{'evidence_count':1})
        recovered = project_snapshot(store,mid)
        assert recovered['health']['identical_failures'] == 0
        assert recovered['health']['stall_warning'] is None


def test_mission_log_pages_older_events_without_changing_current_capacity(tmp_path):
    with BridgeStore(tmp_path/'bridge.sqlite3') as store:
        mission = store.create_mission('log pages','objective',cwd=tmp_path)
        mid = mission['id']
        store.record_runtime_event(mid,'provider-selection',{'provider':'codex','decision':{'usable':True}})
        for index in range(70): store.record_runtime_event(mid,'sample',{'index':index})
        newest = project_snapshot(store,mid)
        older = project_snapshot(store,mid,event_offset=20)
        assert len(newest['events']) == len(older['events']) == 20
        assert older['events'][-1]['id'] < newest['events'][0]['id']
        assert older['capacity']['selection'] == newest['capacity']['selection']
        assert older['event_page']['offset'] == 20


HEARTBEATS = 30_000


def test_operator_events_skip_controller_plumbing_and_stay_fast_with_many_heartbeats(tmp_path) -> None:
    from llm_tools.bridge_monitor import PLUMBING_EVENT_KINDS, latest_operator_events

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission_id = store.create_mission("busy", "objective", cwd=tmp_path, initial_prompt="work")["id"]
    for index in range(25):
        store.record_runtime_event(mission_id, "task-status" if index % 2 else "provider-selection", {"n": index})
    store.record_runtime_event(mission_id, "brand-new-kind", {"n": 99})
    plumbing = sorted(PLUMBING_EVENT_KINDS)
    store.db.executemany(
        "INSERT INTO events(mission_id,task_id,operation_id,kind,data_json,created_at) VALUES(?,?,?,?,?,?)",
        [(mission_id, None, None, plumbing[index % len(plumbing)], "{}", "2026-01-01T00:00:00Z") for index in range(HEARTBEATS)],
    )
    store.db.commit()

    # Bound the work, not the wall clock: SQLite counts virtual-machine steps, so the
    # limit does not move with host load. Scanning every heartbeat row needs at least
    # several steps per row, far above the bound; the indexed lookups need a few hundred.
    steps = [0]

    def count_steps() -> int:
        steps[0] += 1000
        return 0

    store.db.set_progress_handler(count_steps, 1000)
    try:
        snapshot = project_snapshot(store, mission_id)
    finally:
        store.db.set_progress_handler(None, 0)
    recent = snapshot["operator_events"]
    assert len(recent) == 20 and not PLUMBING_EVENT_KINDS & {event["kind"] for event in recent}
    assert [event["id"] for event in recent] == sorted(event["id"] for event in recent)
    assert recent[-1]["kind"] == "brand-new-kind" and recent[-1]["data"] == {"n": 99}
    assert all(event["kind"] in PLUMBING_EVENT_KINDS for event in snapshot["events"]), "events stays the unfiltered latest 20 for compatibility"
    assert steps[0] < HEARTBEATS, f"snapshot ran ~{steps[0]} SQLite steps with {HEARTBEATS} heartbeats; it must not scan them"
    pinned = latest_operator_events(store, mission_id, anchor=recent[0]["id"])
    assert pinned[-1]["id"] == recent[0]["id"]
    plan = " ".join(str(row[3]) for row in store.db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM events INDEXED BY events_mission_kind_id WHERE mission_id=? AND kind=? ORDER BY id DESC LIMIT 20", (mission_id, "task-status")))
    assert "events_mission_kind_id" in plan and "TEMP B-TREE" not in plan
