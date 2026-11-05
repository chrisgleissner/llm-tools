"""Independent checks of native identity, lifecycle and observation boundaries."""

from llm_tools import bridge_agents as agents
from llm_tools.bridge_sessions import codex_session_activity_tail
from llm_tools.bridge_store import BridgeStore


def operation(store, tmp_path, *, native_id="root", status="RUNNING"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mission = store.create_mission("review fixture", "objective", cwd=tmp_path, initial_prompt="work")
    store.set_mission_status(mission["id"], "RUNNING")
    op = store.begin_operation(mission["initial_task_id"], iteration=1, station="codex")
    store.mark_operation_running(op)
    store.db.execute("UPDATE operations SET native_session_id=?,status=?,exit_code=NULL WHERE id=?", (native_id, status, op))
    store.db.commit()
    return mission


def test_native_descendants_require_a_chain_to_the_exact_root(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_codex_command", lambda _env: ["fixture"])
    children = [
        {"id": "grandchild", "parentThreadId": "child", "createdAt": 100, "status": {"type": "active"}},
        {"id": "unrelated", "parentThreadId": "another-root", "createdAt": 101},
        {"id": "cycle-a", "parentThreadId": "cycle-b", "createdAt": 102},
        {"id": "cycle-b", "parentThreadId": "cycle-a", "createdAt": 103},
        {"id": "child", "parentThreadId": "root", "createdAt": 110, "status": {"type": "active"}},
    ]
    def rpc(_command, requests, _env):
        assert all(params.get("includeTurns") is False for _, method, params in requests if method == "thread/read")
        return {ident: {"result": {"thread": {"id": "root"}} if method == "thread/read" else {"data": children}} for ident, method, _ in requests}
    with BridgeStore(tmp_path / "store.sqlite3") as store:
        mission = operation(store, tmp_path)
        result = agents.build_agent_map(store, mission_id=mission["id"], env={}, now=200, codex_rpc=rpc)
    assert {row["native_id"] for row in result["agents"]} == {"root", "child", "grandchild"}
    assert [row["native_id"] for row, _ in agents.ordered_agents(result)] == ["root", "child", "grandchild"]


def test_uncertain_operation_has_no_invented_completion_or_lifetime(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_codex_command", lambda _env: None)
    with BridgeStore(tmp_path / "store.sqlite3") as store:
        operation(store, tmp_path, native_id=None, status="UNCERTAIN")
        row = agents.build_agent_map(store, env={}, now=200)["agents"][0]
    assert row["status"] in {"unknown", "uncertain"}
    assert row["finished_at"] is None and row["age_seconds"] is None
    assert row["model"] == row["reasoning_effort"] == "unknown"


def test_completed_child_without_completion_time_has_unknown_lifetime():
    row = agents._child_record({
        "id": "child", "parentThreadId": "root", "createdAt": 100,
        "status": {"type": "completed"},
    }, mission_id="mission", root_id="root", now=200)
    assert row is not None and row["status"] == "completed"
    assert row["finished_at"] is None and row["age_seconds"] is None
    assert " · ? · completed" in agents.format_agent_map({"agents": [row]}, now=300)[0]


def test_malformed_numeric_creation_times_have_unknown_lifetime():
    for timestamp in (float("nan"), float("inf"), float("-inf"), 10**1000, True):
        row = agents._child_record({
            "id": "child", "parentThreadId": "root", "createdAt": timestamp,
        }, mission_id="mission", root_id="root", now=200)
        assert row is not None and row["age_seconds"] is None


def test_missing_native_response_is_not_fresh_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_codex_command", lambda _env: ["fixture"])
    with BridgeStore(tmp_path / "store.sqlite3") as store:
        operation(store, tmp_path)
        result = agents.build_agent_map(store, env={}, codex_rpc=lambda *_: {})
    assert result["freshness"] in {"partial", "unavailable", "stale", "unknown"}
    assert result["agents"][0]["model"] == "unknown"


def test_same_native_id_in_separate_missions_preserves_both_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_codex_command", lambda _env: None)
    with BridgeStore(tmp_path / "store.sqlite3") as store:
        first = operation(store, tmp_path / "one")
        second = operation(store, tmp_path / "two")
        result = agents.build_agent_map(store, env={})
    ordered = agents.ordered_agents(result)
    assert len(ordered) == 2
    assert {row["mission_id"] for row, _ in ordered} == {first["id"], second["id"]}


def test_route_provider_and_configured_model_use_the_persisted_frozen_config(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_codex_command", lambda _env: None)
    with BridgeStore(tmp_path / "store.sqlite3") as store:
        mission = operation(store, tmp_path)
        store.set_mission_config_values(mission["id"], {
            "routes": "pinned-route",
            "frozen_tool_config": '[routes.pinned-route]\nprovider="codex"\nmodel="pinned-model"\neffort="high"\n',
        }, event_kind="test-config")
        store.db.execute("UPDATE operations SET station='pinned-route',route_id='pinned-route'")
        store.db.commit()
        row = agents.build_agent_map(store, env={})["agents"][0]
    assert row["provider"] == "codex"
    assert row["station"] == "pinned-route"
    assert row["configured_model"] == "pinned-model"
    assert row["configured_effort"] == "high"
    assert row["model"] == row["reasoning_effort"] == "unknown"


def test_root_navigation_requires_an_existing_live_station(tmp_path, monkeypatch):
    from llm_tools import bridge_missions

    monkeypatch.setattr(agents, "_codex_command", lambda _env: None)
    with BridgeStore(tmp_path / "store.sqlite3") as store:
        operation(store, tmp_path)
        monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: False)
        offline = agents.build_agent_map(store, env={})["agents"][0]
        assert offline["capabilities"]["inspect"] is True
        assert offline["capabilities"]["navigate"] is False
        monkeypatch.setattr(bridge_missions, "station_live", lambda _mission_id: True)
        online = agents.build_agent_map(store, env={})["agents"][0]
        assert online["capabilities"]["navigate"] is True


def test_agent_ordering_handles_cycles_and_deep_parent_chains():
    rows = [{"id": "node-0", "parent_id": None, "created_at": 0}]
    rows.extend({"id": f"node-{index}", "parent_id": f"node-{index - 1}", "created_at": index} for index in range(1, 1500))
    rows.extend([
        {"id": "cycle-a", "parent_id": "cycle-b", "created_at": 2000},
        {"id": "cycle-b", "parent_id": "cycle-a", "created_at": 2001},
    ])
    ordered = agents.ordered_agents({"agents": rows})
    assert len(ordered) == len(rows)
    assert max(depth for _, depth in ordered) <= 64
    assert len({row["id"] for row, _ in ordered}) == len(rows)


def test_codex_rpc_discards_an_oversized_response_record(monkeypatch):
    class Pipe:
        def write(self, _value):
            return None

        def flush(self):
            return None

        def readline(self, limit):
            return response[:limit]

    class Process:
        stdin = Pipe()
        stdout = Pipe()

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 0

    response = __import__("json").dumps({"id": 1, "result": {"value": "x" * 70000}}).encode() + b"\n"
    monkeypatch.setattr(agents.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    result = agents._codex_rpc(["fixture"], [(1, "thread/read", {})], {})
    assert result == {}


def test_codex_activity_reads_only_exact_parent_bound_tool_metadata(tmp_path):
    root_id = "12345678-1234-4234-8234-123456789abc"
    child_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    sessions = tmp_path / "sessions"
    child_cwd = tmp_path / "child-worktree"
    child_cwd.mkdir()
    transcript = sessions / f"{child_id}.jsonl"
    sessions.mkdir()
    rows = [
        {"type": "session_meta", "payload": {"id": child_id, "cwd": str(child_cwd), "parentThreadId": root_id}},
        {"type": "response_item", "timestamp": "2026-11-05T05:00:00Z", "payload": {
            "type": "custom_tool_call", "name": "exec", "arguments": "private prompt text",
        }},
    ]
    transcript.write_text("\n".join(__import__("json").dumps(row) for row in rows) + "\n", encoding="utf-8")
    observed = codex_session_activity_tail(transcript, session_id=child_id, parent_id=root_id, sessions_root=sessions)
    assert observed == {
        "activity": "last observed tool: exec", "timestamp": "2026-11-05T05:00:00Z",
        "lifecycle": None, "lifecycle_timestamp": None,
    }
    assert "private prompt text" not in repr(observed)
    assert codex_session_activity_tail(transcript, session_id=child_id, parent_id="other-root", sessions_root=sessions) is None
