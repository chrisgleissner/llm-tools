"""Agent-map formatting and bounded observer contracts."""

import json

from llm_tools import bridge_agents as agents
from llm_tools import bridge
from llm_tools.bridge_actions import actions_for
from llm_tools.bridge_store import BridgeStore


def _running_codex(store, tmp_path, *, native_id="root"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    mission = store.create_mission("agent map fixture", "objective", cwd=tmp_path, initial_prompt="work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(mission["initial_task_id"], iteration=1, station="codex")
    store.mark_operation_running(operation_id)
    store.db.execute("UPDATE operations SET native_session_id=?,model='configured-model' WHERE id=?", (native_id, operation_id))
    store.db.commit()
    return mission


def test_agent_map_uses_exact_read_only_rpc_and_separates_observed_from_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_codex_command", lambda _env: ["fixture-app-server"])
    observed_requests = []

    def rpc(command, requests, _env):
        assert command == ["fixture-app-server"]
        observed_requests.extend(requests)
        result = {}
        for request_id, method, params in requests:
            if method == "thread/read":
                assert params == {"threadId": "root", "includeTurns": False}
                result[request_id] = {"result": {"thread": {
                    "id": "root", "model": "observed-root", "reasoningEffort": "medium",
                }}}
            else:
                assert params["ancestorThreadId"] == "root"
                result[request_id] = {"result": {"data": [
                    {"id": "child", "parentThreadId": "root", "createdAt": 100,
                     "updatedAt": 140, "model": "observed-child", "reasoningEffort": "high",
                     "agentNickname": "random-name", "agentRole": "reviewer", "name": "Thread title",
                     "preview": "private first prompt",
                     "status": {"type": "completed"}},
                ]}}
        return result

    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = _running_codex(store, tmp_path / "repo")
        result = agents.build_agent_map(store, mission_id=mission["id"], env={}, now=200, codex_rpc=rpc)

    ordered = agents.ordered_agents(result)
    assert [row["kind"] for row, _ in ordered] == ["bridge-root", "native-child"]
    root, child = (row for row, _ in ordered)
    assert root["model"] == "observed-root" and root["model_source"] == "observed"
    assert root["configured_model"] == "configured-model"
    assert root["reasoning_effort"] == "medium" and root["effort_source"] == "observed"
    assert child["model"] == "observed-child" and child["reasoning_effort"] == "high"
    assert child["name"] == "Thread title"
    assert child["role"] == "reviewer" and child["role_source"] == "observed"
    assert "private first prompt" not in repr(child)
    assert child["finished_at"] == 140 and child["age_seconds"] == 40
    assert root["id"] in child["parent_id"]
    assert all(method in {"thread/read", "thread/list"} for _, method, _ in observed_requests)
    assert result["freshness"] == "fresh"


def test_finished_child_age_stays_frozen_after_snapshot(tmp_path):
    data = {
        "observed_at": 200,
        "agents": [{
            "id": "child", "kind": "native-child", "provider": "codex", "name": "reviewer",
            "parent_id": None, "root_id": "child", "created_at": 100, "finished_at": 140,
            "age_seconds": 40, "status": "completed", "activity": "finished",
            "model": "observed-model", "model_source": "observed", "reasoning_effort": "unknown",
            "effort_source": "unknown", "capabilities": {"inspect": True, "navigate": False},
        }],
    }
    first = agents.format_agent_map(data, now=210)
    later = agents.format_agent_map(data, now=360)
    assert first == later


def test_unfinished_agent_age_advances_locally_without_refresh(tmp_path):
    data = {
        "observed_at": 200,
        "agents": [{
            "id": "root", "kind": "bridge-root", "provider": "codex", "name": "worker",
            "parent_id": None, "root_id": "root", "created_at": 100, "finished_at": None,
            "age_seconds": 100, "status": "running", "activity": "working",
            "model": "unknown", "model_source": "unknown", "configured_model": "unknown",
            "reasoning_effort": "unknown", "effort_source": "unknown", "configured_effort": "unknown",
        }],
    }
    assert "2m" in agents.format_agent_map(data, now=260)[0]


def test_agents_cli_is_read_only_and_scoped_for_managed_callers(monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_MANAGED", "1")
    monkeypatch.setenv("LLM_BRIDGE_MISSION_ID", "owned")
    parser = bridge._build_parser(default_mission=None)
    own = parser.parse_args(["agents"])
    all_missions = parser.parse_args(["agents", "--all-missions"])
    suspend = parser.parse_args(["suspend-until-credits", "--mission", "owned"])
    assert bridge._managed_allows(own) and not bridge._managed_scope_violation(own)
    assert bridge._managed_allows(all_missions) and bridge._managed_scope_violation(all_missions)
    assert not bridge._managed_allows(suspend)


def test_suspend_action_has_explicit_label_and_slash_command():
    action = next(item for item in actions_for("STOPPED") if item.command == "suspend-until-credits")
    assert action.label == "Suspend until credits recover"
    assert action.typed == "/suspend-until-credits"


def test_not_loaded_child_uses_exact_bounded_activity_without_claiming_it_is_live(tmp_path, monkeypatch):
    root_id = "12345678-1234-4234-8234-123456789abc"
    child_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    sessions_root = tmp_path / "codex-home" / "sessions"
    child_cwd = tmp_path / "separate-child-worktree"
    child_cwd.mkdir()
    transcript = sessions_root / "2026" / f"{child_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    header = {"type": "session_meta", "payload": {"id": child_id, "cwd": str(child_cwd), "parentThreadId": root_id}}
    started = {"timestamp": "1970-01-01T00:01:50Z", "type": "event_msg", "payload": {"type": "task_started", "prompt": "do not retain"}}
    complete = {"timestamp": "1970-01-01T00:02:20Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "do not retain"}}
    tool = {"timestamp": "1970-01-01T00:02:25Z", "type": "response_item", "payload": {
        "type": "custom_tool_call", "name": "exec", "arguments": "do not retain",
    }}
    transcript.write_text("\n".join(json.dumps(item) for item in (header, started, complete, tool)) + "\n", encoding="utf-8")
    monkeypatch.setattr(agents, "_codex_command", lambda _env: ["fixture"])

    def rpc(_command, requests, _env):
        return {
            request_id: {"result": {"thread": {"id": root_id}} if method == "thread/read" else {"data": [
                {"id": child_id, "parentThreadId": root_id, "createdAt": 100, "status": {"type": "notLoaded"}},
            ]}}
            for request_id, method, _params in requests
        }

    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = _running_codex(store, tmp_path / "repo", native_id=root_id)
        env = {"CODEX_HOME": str(sessions_root.parent)}
        result = agents.build_agent_map(store, mission_id=mission["id"], env=env, now=200, codex_rpc=rpc)
        child = next(row for row in result["agents"] if row["kind"] == "native-child")
        assert child["status"] == "completed"
        assert child["native_status"] == "notLoaded"
        assert child["finished_at"] == "1970-01-01T00:02:20Z"
        assert child["age_seconds"] == 40
        assert child["lifecycle_event"] == "task_complete"
        assert child["lifecycle_at"] == "1970-01-01T00:02:20Z"
        assert child["activity"] == "last observed tool: exec"
        assert child["last_activity_at"] == "1970-01-01T00:02:25Z"
        assert child["lifecycle_source"] == "bounded exact Codex session events"
        assert child["freshness"] == "partial"
        assert "do not retain" not in repr(child)

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": "1970-01-01T00:03:10Z", "type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        reopened = agents.build_agent_map(store, mission_id=mission["id"], env=env, now=220, codex_rpc=rpc)
        current = next(row for row in reopened["agents"] if row["kind"] == "native-child")
        assert current["status"] == "active"
        assert current["native_status"] == "notLoaded"
        assert current["finished_at"] is None
        assert current["age_seconds"] == 120
        assert current["activity"] == "last provider event: task started"
        assert current["activity_age_seconds"] == 30

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": "1970-01-01T00:03:00Z", "type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
        out_of_order = agents.build_agent_map(store, mission_id=mission["id"], env=env, now=230, codex_rpc=rpc)
        after_out_of_order = next(row for row in out_of_order["agents"] if row["kind"] == "native-child")
        assert after_out_of_order["status"] == "active"
        assert after_out_of_order["age_seconds"] == 130

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": "1970-01-01T00:03:30Z", "type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
        completed = agents.build_agent_map(store, mission_id=mission["id"], env=env, now=240, codex_rpc=rpc)
        completed_child = next(row for row in completed["agents"] if row["kind"] == "native-child")
        later = agents.build_agent_map(store, mission_id=mission["id"], env=env, now=260, codex_rpc=rpc)
        later_child = next(row for row in later["agents"] if row["kind"] == "native-child")
        assert completed_child["status"] == later_child["status"] == "completed"
        assert completed_child["age_seconds"] == later_child["age_seconds"] == 110
        stale_completion = agents.build_agent_map(store, mission_id=mission["id"], env=env, now=1000, codex_rpc=rpc)
        stale_completed = next(row for row in stale_completion["agents"] if row["kind"] == "native-child")
        assert stale_completed["status"] == "completed"
        assert stale_completed["age_seconds"] == 110
        assert stale_completed["freshness"] == "stale"

        with transcript.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"timestamp": "1970-01-01T00:04:10Z", "type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        resumed = agents.build_agent_map(store, mission_id=mission["id"], env=env, now=270, codex_rpc=rpc)
        resumed_child = next(row for row in resumed["agents"] if row["kind"] == "native-child")
        assert resumed_child["status"] == "active"
        assert resumed_child["finished_at"] is None
        assert resumed_child["age_seconds"] == 170

        stale = agents.build_agent_map(store, mission_id=mission["id"], env=env, now=1000, codex_rpc=rpc)
        stale_child = next(row for row in stale["agents"] if row["kind"] == "native-child")
        assert stale_child["status"] == "notLoaded"
        assert stale_child["age_seconds"] == 900
        assert stale_child["freshness"] == "stale"
        assert stale["freshness"] == "partial"


def test_recent_tool_activity_does_not_refresh_old_lifecycle_or_claim_active(tmp_path, monkeypatch):
    root_id = "12345678-1234-4234-8234-123456789abc"
    child_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    sessions_root = tmp_path / "codex-home" / "sessions"
    child_cwd = tmp_path / "child-worktree"
    child_cwd.mkdir()
    transcript = sessions_root / f"{child_id}.jsonl"
    sessions_root.mkdir(parents=True)
    header = {"type": "session_meta", "payload": {"id": child_id, "cwd": str(child_cwd), "parentThreadId": root_id}}
    started = {"timestamp": "1970-01-01T00:00:50Z", "type": "event_msg", "payload": {"type": "task_started"}}
    tool = {"timestamp": "1970-01-01T00:03:15Z", "type": "response_item", "payload": {
        "type": "custom_tool_call", "name": "exec", "arguments": "do not retain",
    }}
    transcript.write_text("\n".join(json.dumps(item) for item in (header, started, tool)) + "\n", encoding="utf-8")
    monkeypatch.setattr(agents, "_codex_command", lambda _env: ["fixture"])

    def rpc(_command, requests, _env):
        return {
            request_id: {"result": {"thread": {"id": root_id}} if method == "thread/read" else {"data": [
                {"id": child_id, "parentThreadId": root_id, "createdAt": 10, "status": {"type": "notLoaded"}},
            ]}}
            for request_id, method, _params in requests
        }

    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = _running_codex(store, tmp_path / "repo", native_id=root_id)
        result = agents.build_agent_map(
            store, mission_id=mission["id"], env={"CODEX_HOME": str(sessions_root.parent)}, now=200,
            codex_rpc=rpc,
        )
    child = next(row for row in result["agents"] if row["kind"] == "native-child")
    assert child["status"] == child["native_status"] == "notLoaded"
    assert child["activity"] == "last observed tool: exec"
    assert child["activity_age_seconds"] == 5 and child["activity_freshness"] == "fresh"
    assert child["lifecycle_age_seconds"] == 150 and child["lifecycle_freshness"] == "stale"
    assert child["freshness"] == "stale"
    assert "do not retain" not in repr(child)


def test_agent_map_resolves_short_unique_mission_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_codex_command", lambda _env: None)
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = _running_codex(store, tmp_path / "repo")
        result = agents.build_agent_map(store, mission_id=mission["id"][:8], env={}, now=200)
    assert {row["mission_id"] for row in result["agents"]} == {mission["id"]}


def test_agent_map_rejects_ambiguous_and_unknown_mission_prefixes(tmp_path, monkeypatch):
    from llm_tools import bridge_missions

    monkeypatch.setattr(agents, "_codex_command", lambda _env: None)
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission = _running_codex(store, tmp_path / "repo")
        original = store.missions_with_id_prefix
        monkeypatch.setattr(store, "missions_with_id_prefix", lambda ref: [
            *original(ref), {"id": ref + "00000000000000000000000000000000", "name": "second", "status": "RUNNING"},
        ])
        monkeypatch.setattr(bridge_missions, "mission_choices", lambda rows: "ambiguous fixture")
        try:
            agents.build_agent_map(store, mission_id=mission["id"][:8], env={})
        except ValueError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("ambiguous prefix was accepted")

        monkeypatch.setattr(store, "missions_with_id_prefix", original)
        try:
            agents.build_agent_map(store, mission_id="deadbeef", env={})
        except bridge_missions.MissionNotFound:
            pass
        else:
            raise AssertionError("unknown prefix was accepted")
