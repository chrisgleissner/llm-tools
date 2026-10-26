"""Controller-owned refresh observation stays read-only and stoppable."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from llm_tools.bridge_refresh import CapacityRefreshWorker, refresh_capacity_observation
from llm_tools.bridge_store import BridgeStore


def test_refresh_worker_records_heartbeat_then_delays_quota_refresh(tmp_path) -> None:
    database = tmp_path / "bridge.sqlite3"
    store = BridgeStore(database)
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    calls: list[str] = []

    def selector(_mission):
        calls.append("refresh")
        return {"provider": "codex", "observed_at": "now"}

    worker = CapacityRefreshWorker(mission["id"], database, interval_seconds=5, heartbeat_seconds=1, selector=selector)
    worker.start()
    time.sleep(0.1)
    worker.stop()

    events = store.events(mission["id"])
    heartbeat = next(event for event in events if event["kind"] == "controller-heartbeat")
    assert heartbeat["data"]["pid"] > 0
    assert heartbeat["data"]["process_start"]
    assert heartbeat["data"]["owner_lock_held"] is True
    assert heartbeat["data"]["desired_mission_status"] == "CREATED"
    assert not any(event["kind"] == "capacity-refresh" for event in events)
    assert calls == []


def test_refresh_worker_records_read_only_observation_after_first_interval(tmp_path) -> None:
    database = tmp_path / "bridge.sqlite3"
    store = BridgeStore(database)
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    refreshed = threading.Event()

    def selector(_mission):
        refreshed.set()
        return {"provider": "codex", "decision": {"usable": True}}

    worker = CapacityRefreshWorker(
        mission["id"], database, interval_seconds=5, heartbeat_seconds=1,
        initial_refresh_delay_seconds=0.02, selector=selector,
    )
    worker.start()
    assert refreshed.wait(1)
    worker.stop()

    observations = [event for event in store.events(mission["id"]) if event["kind"] == "capacity-refresh"]
    assert len(observations) == 1
    assert observations[0]["data"]["provider"] == "codex"


def test_refresh_worker_waits_for_inflight_selection_then_stops_without_writes(tmp_path) -> None:
    database = tmp_path / "bridge.sqlite3"
    store = BridgeStore(database)
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def selector(_mission):
        entered.set()
        release.wait(2)
        return {"provider": "codex", "observed_at": "now"}

    worker = CapacityRefreshWorker(mission["id"], database, interval_seconds=5, heartbeat_seconds=1, initial_refresh_delay_seconds=0, selector=selector)
    worker.start()
    assert entered.wait(1)
    stopper = threading.Thread(target=worker.stop)
    stopper.start()
    time.sleep(0.05)
    release.set()
    stopper.join(2)
    assert not stopper.is_alive()

    events = store.events(mission["id"])
    assert not any(event["kind"] == "capacity-refresh" for event in events)
    assert not any(event["kind"] == "capacity-refresh-error" for event in events)


def test_capacity_observation_reuses_ralph_selection_and_sanitizes_provider_data(monkeypatch) -> None:
    from llm_tools import config as toolconfig
    from llm_tools import ralph_robin

    cfg = SimpleNamespace(route_policies={}, providers=[], routes=[])
    monkeypatch.setattr(toolconfig, "load_config", lambda: {"credential": "do-not-record"})
    monkeypatch.setattr(toolconfig, "merged_tool_config", lambda _raw, _section: {})
    monkeypatch.setattr(ralph_robin, "RalphConfig", lambda: cfg)
    monkeypatch.setattr(ralph_robin, "parse_providers", lambda _spec: ["codex"])
    monkeypatch.setattr(ralph_robin, "resolve_policies", lambda *_args: None)
    calls = []
    selection = {
        "provider": "codex",
        "route": "codex-spark",
        "selected_model": "spark",
        "rotation_reason": "current-usable",
        "all_rate_limited": False,
        "decision": {"usable": True, "reason": "ready", "token": "secret"},
        "decisions": [{
            "provider": "codex", "route": "codex-spark", "usable": True,
            "reason": "ready", "private_payload": "must not persist",
            "windows": [{"name": "5h", "kind": "reset_window", "remaining_percent": 80, "api_key": "secret"}],
        }],
    }

    def select(actual_cfg, logs, index, skipped):
        calls.append((actual_cfg, logs, index, skipped))
        return selection

    monkeypatch.setattr(ralph_robin, "select_provider", select)
    observation = refresh_capacity_observation({"config": {"providers": "codex", "routes": ""}})

    assert len(calls) == 1
    assert calls[0][0] is cfg and calls[0][2:] == (0, set())
    assert observation["provider"] == "codex"
    assert observation["decisions"][0]["windows"][0]["remaining_percent"] == 80
    assert "token" not in observation["decision"]
    assert "private_payload" not in observation["decisions"][0]
    assert "api_key" not in observation["decisions"][0]["windows"][0]
    assert "credential" not in str(observation)


def test_capacity_observation_resolves_frozen_route_policy_without_launching(monkeypatch) -> None:
    from llm_tools import config as toolconfig
    from llm_tools import ralph_robin, routes

    cfg = SimpleNamespace(route_policies={}, providers=[], routes=[])
    frozen = {"routes": {"codex-spark": {"provider": "codex", "model": "spark"}}}
    policy = {"id": "codex-spark", "provider": "codex", "model": "spark"}
    monkeypatch.setattr(toolconfig, "load_config", lambda: frozen)
    monkeypatch.setattr(toolconfig, "merged_tool_config", lambda _raw, _section: {})
    monkeypatch.setattr(ralph_robin, "RalphConfig", lambda: cfg)
    monkeypatch.setattr(ralph_robin, "parse_routes_spec", lambda _spec: ["codex-spark"])
    monkeypatch.setattr(ralph_robin, "resolve_policies", lambda actual, _raw: actual.route_policies)
    monkeypatch.setattr(routes, "route_or_implicit_provider", lambda raw, name: policy if raw is frozen and name == "codex-spark" else None)
    selected = []
    monkeypatch.setattr(ralph_robin, "select_provider", lambda actual, *_args: selected.append(actual.route_policies.copy()) or {
        "provider": "codex", "route": "codex-spark", "decision": {"usable": True}, "decisions": [],
    })

    observation = refresh_capacity_observation({"config": {"providers": "", "routes": "codex-spark"}})

    assert cfg.routes == ["codex-spark"]
    assert selected == [{"codex-spark": policy}]
    assert observation["route"] == "codex-spark"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval_seconds": 4},
        {"heartbeat_seconds": 0},
        {"initial_refresh_delay_seconds": -1},
        {"initial_refresh_delay_seconds": 3601},
    ],
)
def test_refresh_worker_rejects_invalid_intervals(tmp_path, kwargs) -> None:
    with pytest.raises(ValueError):
        CapacityRefreshWorker("mission", tmp_path / "bridge.sqlite3", **kwargs)


def test_refresh_failure_event_is_bounded_and_does_not_leak_exception_text(tmp_path) -> None:
    database = tmp_path / "bridge.sqlite3"
    store = BridgeStore(database)
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    failed = threading.Event()

    def selector(_mission):
        raise TimeoutError("sensitive credential details")

    worker = CapacityRefreshWorker(
        mission["id"], database, interval_seconds=5, heartbeat_seconds=1,
        initial_refresh_delay_seconds=0, selector=selector,
    )
    worker.start()
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if any(event["kind"] == "capacity-refresh-error" for event in store.events(mission["id"])):
                failed.set()
                break
            time.sleep(0.01)
        assert failed.is_set()
    finally:
        worker.stop()

    event = next(event for event in store.events(mission["id"]) if event["kind"] == "capacity-refresh-error")
    assert event["data"]["reason"] == "TimeoutError"
    assert "credential" not in str(event["data"])


def test_capacity_projection_reuses_usage_forecast_without_inventing_opaque_values(monkeypatch):
    from llm_tools import common
    from llm_tools.bridge_refresh import _safe_decision
    observed = []
    def estimate(provider, scope, remaining):
        observed.append((provider, scope, remaining))
        return '38m' if scope == '5h' else '-'
    monkeypatch.setattr(common, 'estimate_remaining_time_from_log', estimate)
    result = _safe_decision({'decisions':[{'provider':'codex','windows':[
        {'name':'5h','kind':'reset_window','remaining_percent':25},
        {'name':'weekly','kind':'reset_window','remaining_percent':71},
        {'name':'subscription','kind':'opaque','remaining_percent':None}]}]})
    windows = result['decisions'][0]['windows']
    assert windows[0]['estimated_remaining'] == '38m'
    assert windows[1]['estimated_remaining'] is None
    assert windows[2].get('estimated_remaining') is None
    assert observed == [('Codex','5h',25),('Codex','weekly',71)]


def test_live_capacity_observations_warm_real_forecast_history_without_usage_view(tmp_path, monkeypatch):
    import json
    from llm_tools import common
    from llm_tools.bridge_refresh import _safe_decision
    monkeypatch.setenv('XDG_CACHE_HOME',str(tmp_path/'cache'))
    monkeypatch.setenv('LLM_USAGE_NOW_EPOCH','100000')
    decision = {'provider':'codex','windows':[{'name':'5h','kind':'reset_window','remaining_percent':90}]}
    assert _safe_decision({'decisions':[decision]})['decisions'][0]['windows'][0]['estimated_remaining'] is None
    monkeypatch.setenv('LLM_USAGE_NOW_EPOCH','101000')
    decision['windows'][0]['remaining_percent'] = 80
    result = _safe_decision({'decisions':[decision]})
    assert result['decisions'][0]['windows'][0]['estimated_remaining'] == common.fmt_duration(8000)
    rows = [json.loads(line) for line in (common.usage_cache_dir()/'llm-usage.log').read_text().splitlines()]
    assert [row['remaining'] for row in rows] == [90,80]


def test_forecast_pool_identity_preserves_actual_model_and_delegated_capacity(monkeypatch, tmp_path):
    import json
    from llm_tools import common
    from llm_tools.bridge_refresh import _safe_decision
    monkeypatch.setenv('XDG_CACHE_HOME',str(tmp_path/'cache'))
    codex_snapshot = {'available':True,'rows':[{'key':'codex-spark','name':'Codex Spark capacity pool',
        'five_hour':{'used':40},'week':{'used':30}}]}
    spark = common.usage_decision_for_provider('codex','auto','1','60',codex_snapshot,
        model='gpt-spark',allow_fallback=False)
    assert spark['usage_history_provider'] == 'Codex Spark capacity pool'
    claude_snapshot = {'available':True,'model_weeks':[{'model':'Sonnet','week':{'used':20}}]}
    sonnet = common.usage_decision_for_provider('claude','auto','1','60',claude_snapshot,
        model='claude-sonnet',allow_fallback=False)
    assert sonnet['usage_history_provider'] == 'Claude Sonnet'
    delegated = {'provider':'kilo','capacity_provider':'zai','usage_history_provider':'Z.ai',
        'selected_model':'zai/glm-5.2','windows':[{'name':'5h','kind':'reset_window','remaining':50}]}
    _safe_decision({'decisions':[spark,sonnet,delegated]})
    rows = [json.loads(line) for line in (common.usage_cache_dir()/'llm-usage.log').read_text().splitlines()]
    assert {row['provider'] for row in rows} == {'Codex Spark capacity pool','Claude Sonnet','Z.ai'}


def test_rapid_identical_observations_are_sampled_once_per_30_seconds(tmp_path, monkeypatch):
    import json
    from llm_tools import common
    from llm_tools.bridge_refresh import _safe_decision
    monkeypatch.setenv('XDG_CACHE_HOME',str(tmp_path/'cache'))
    monkeypatch.setenv('LLM_USAGE_NOW_EPOCH','120000')
    selection = {'decisions':[{'provider':'codex','windows':[{'name':'5h','kind':'reset_window','remaining_percent':80}]}]}
    for _ in range(20): _safe_decision(selection)
    log = common.usage_cache_dir()/'llm-usage.log'
    assert len(log.read_text().splitlines()) == 1
    monkeypatch.setenv('LLM_USAGE_NOW_EPOCH','120030')
    _safe_decision(selection)
    assert len(log.read_text().splitlines()) == 2
    selection['decisions'][0]['windows'][0]['remaining_percent'] = 79
    _safe_decision(selection)
    assert json.loads(log.read_text().splitlines()[-1])['remaining'] == 79
