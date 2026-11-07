"""session.list and usage.snapshot: read-only operations for the web Sessions and Usage views."""

from __future__ import annotations

import json
import time

import pytest

from llm_tools import bridge_commands as commands, bridge_worker as worker
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandRequest
from llm_tools.bridge_protocol import Message
from llm_tools.bridge_store import BridgeStore

SESSION_ID = "0b0f6d3e-1c2a-4f5b-8d7e-123456789abc"
TOKEN = "t" * 40


@pytest.fixture
def mission(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    assert main(["mission", "create", "--name", "Visible name", "--prompt", "Secret objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    mission_id = json.loads(capsys.readouterr().out)["mission"]["id"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "RUNNING")
        task_id = store.list_tasks(mission_id)[0]["id"]
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.record_operation_session(operation, restoration_state="NATIVE_CREATED", provider="codex", session_id=SESSION_ID, cwd=str(tmp_path), source="codex:session_meta")
    return state, mission_id, operation


def test_session_list_reports_metadata_only(mission):
    _, mission_id, operation = mission
    result = commands.execute(CommandRequest("session.list"))
    assert result.outcome == "ok"
    (item,) = result.result["sessions"]
    assert item["operation_id"] == operation and item["mission_id"] == mission_id
    assert (item["provider"], item["native_session_id"], item["task_ordinal"]) == ("codex", SESSION_ID, 1)
    assert item["restoration_state"] == "NATIVE_CREATED" and item["started_at"] and item["last_activity_at"]
    assert set(item) == {"operation_id", "mission_id", "mission_name", "task_id", "task_ordinal", "provider", "route_id", "model", "state",
                         "restoration_state", "native_session_id", "started_at", "last_activity_at"}
    assert "Secret objective" not in json.dumps(result.result)


def test_session_list_filters_by_mission_prefix_and_validates(mission):
    _, mission_id, _ = mission
    assert len(commands.execute(CommandRequest("session.list", {"mission": mission_id[:8]})).result["sessions"]) == 1
    assert commands.execute(CommandRequest("session.list", {"mission": "zz-none"})).outcome == "rejected"
    for limit in (0, 1001):
        assert commands.execute(CommandRequest("session.list", {"limit": limit})).error.code == "invalid-argument"


def test_session_list_supports_stable_offset_pages(mission):
    state, mission_id, _ = mission
    with BridgeStore(state / "bridge.sqlite3") as store:
        task_id = store.add_task(mission_id, "Second task", title="Second task")
        store.begin_operation(task_id, iteration=2, station="claude")
    full = commands.execute(CommandRequest("session.list", {"mission": mission_id, "limit": 10, "offset": 0}))
    result = commands.execute(CommandRequest("session.list", {"mission": mission_id, "limit": 1, "offset": 1}))
    assert result.outcome == "ok"
    assert result.result["total"] == 2 and result.result["offset"] == 1
    assert len(result.result["sessions"]) == 1
    assert result.result["sessions"] == full.result["sessions"][1:2]


def test_session_list_with_no_missions(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "empty"))
    assert commands.execute(CommandRequest("session.list")).result == {"schema_version": 1, "sessions": [], "total": 0, "offset": 0}


def _inject_codex(monkeypatch):
    now = int(time.time())
    monkeypatch.setenv("LLM_USAGE_PROVIDERS", "codex")
    monkeypatch.setenv("LLM_USAGE_NO_SERVICE", "1")
    monkeypatch.setenv("LLM_USAGE_DISABLE_CODEX_APP_SERVER", "1")
    monkeypatch.setenv("LLM_USAGE_CODEX_RATE_LIMITS_JSON", json.dumps({"rateLimits": {
        "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": now + 3600},
        "secondary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": now + 86400}}}))


def test_usage_snapshot_reports_normalized_figures(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "s"))
    _inject_codex(monkeypatch)
    result = commands.execute(CommandRequest("usage.snapshot"))
    assert result.outcome == "ok" and result.result["available"] is True and result.result["source"] == "direct"
    (provider,) = result.result["providers"]
    assert (provider["provider"], provider["available"], provider["ready"]) == ("codex", True, True)
    scopes = {scope["scope"]: scope for scope in provider["scopes"]}
    assert (scopes["5h"]["remaining_percent"], scopes["5h"]["used_percent"]) == (75.0, 25.0)
    assert scopes["weekly"]["used_percent"] == 10.0 and isinstance(scopes["weekly"]["reset_epoch"], int)
    assert "rateLimits" not in json.dumps(result.result)


def test_usage_snapshot_degrades_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "s"))
    from llm_tools import usage

    def boom(*args, **kwargs):
        raise RuntimeError("secret-token-value")

    monkeypatch.setattr(usage, "_render_data_for_frame", boom)
    result = commands.execute(CommandRequest("usage.snapshot"))
    assert result.outcome == "ok" and result.result["available"] is False
    assert result.result["reason"] == "read-failed:RuntimeError" and result.result["providers"] == []
    assert "secret-token-value" not in json.dumps(result.result)


def test_usage_snapshot_times_out(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "s"))
    import threading
    from llm_tools import usage

    release = threading.Event()
    monkeypatch.setattr(usage, "_render_data_for_frame", lambda *a, **k: release.wait(5) and None)
    monkeypatch.setattr(commands, "_USAGE_TIMEOUT_SECONDS", 0.05)
    try:
        result = commands.execute(CommandRequest("usage.snapshot"))
    finally:
        release.set()
    assert result.result["available"] is False and result.result["reason"] == "timeout"


def test_read_only_worker_serves_both_operations(mission, monkeypatch):
    state, _, _ = mission
    _inject_codex(monkeypatch)
    config = worker.enroll("ws-1", "wss://coord.home.arpa:8443", TOKEN, ca_pem=None, allow_control=False)
    session = worker.WorkerSession(config, open_store=lambda: BridgeStore(state / "bridge.sqlite3"), software_version="9.9", save=lambda value: worker._save(value))
    session.handle(Message("welcome", "coordinator", 1, {"generation": 1, "protocol_version": 1, "event_cursor": 0}))
    for operation in ("session.list", "usage.snapshot"):
        _, result = session.handle(Message("command", config.worker_id, session.generation, CommandRequest(operation).to_json()))
        assert result.payload["outcome"] == "ok", (operation, result.payload)
    catalog = {entry["operation"]: entry for entry in commands.operation_catalog()}
    assert catalog["session.list"]["mutating"] is False and catalog["usage.snapshot"]["needs_mission"] is False
