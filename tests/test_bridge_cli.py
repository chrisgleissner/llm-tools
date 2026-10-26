"""Public LLM Bridge CLI contracts."""

from __future__ import annotations

import json
import io
from pathlib import Path

from llm_tools.bridge import dispatch_console_command, main, status_snapshot
from llm_tools.bridge_store import BridgeStore, MissionLock


def test_mission_cli_create_status_tasks_events_and_evidence(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    objective = tmp_path / "objective.md"
    objective.write_text("Implement durable bridge\nKeep the original text.", encoding="utf-8")

    assert main(["mission", "create", "--file", str(objective), "--cwd", str(tmp_path), "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    mission_id = created["mission"]["id"]
    task_id = created["task"]["id"]
    assert created["task"]["prompt"] == objective.read_text(encoding="utf-8")

    assert main(["status", "--mission", mission_id, "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["mission"]["id"] == mission_id
    assert status["tasks"][0]["id"] == task_id
    assert status["tasks"][0]["status"] == "QUEUED"

    assert main(["task", "verify", "--mission", mission_id, "--task", str(task_id), "--approve"]) == 2
    assert "not awaiting verification" in capsys.readouterr().err

    store = BridgeStore(state / "bridge.sqlite3")
    store.set_mission_status(mission_id, "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    assert main(["task", "verify", "--mission", mission_id, "--task", str(task_id), "--approve", "--json"]) == 0
    capsys.readouterr()
    verified = store.get_task(task_id)
    assert verified["status"] == "COMPLETED"
    assert verified["evidence"][-1]["source"] == "approval"

    assert main(["events", "--mission", mission_id, "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    assert any(event["kind"] == "task-verified" for event in events["events"])


def test_legacy_prompt_file_creates_task_one_and_console_command_is_safe(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    objective = tmp_path / "objective.md"
    objective.write_text("Build a native terminal bridge", encoding="utf-8")
    assert main(["--prompt-file", str(objective), "--cwd", str(tmp_path), "--no-start"]) == 0
    result = json.loads(capsys.readouterr().out)
    mission_id = result["mission"]["id"]
    snapshot = status_snapshot(mission_id)
    assert snapshot["tasks"][0]["prompt"] == "Build a native terminal bridge"
    assert snapshot["tasks"][0]["id"] == result["task"]["id"]


def test_console_cancel_task_id_does_not_cancel_other_mission_work(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "objective", cwd=tmp_path)
        task_to_cancel = store.add_task(mission["id"], "cancel this queued task")
        active_task = store.add_task(mission["id"], "keep active work")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(active_task, iteration=1, station="codex")
        store.mark_operation_running(operation)

    response = json.loads(dispatch_console_command(mission["id"], f"cancel {task_to_cancel}"))

    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission["id"])["status"] == "RUNNING"
        assert store.get_task(task_to_cancel)["status"] == "CANCELLED"
        assert store.get_task(active_task)["status"] == "RUNNING"
        assert store.get_operation(operation)["status"] == "RUNNING"
    assert response["status"] == "APPLIED"


def test_mission_create_accepts_stdin_and_persists_verifier_argv(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setattr("sys.stdin", io.StringIO("verbatim\nobjective\n"))

    assert main([
        "mission", "create", "--file", "-", "--cwd", str(tmp_path), "--native",
        "--verifier-arg", "python", "--verifier-arg=-c", "--verifier-arg",
        "import json; print(json.dumps({'passed': True}))", "--verifier-timeout", "17", "--json",
        "--capacity-refresh-seconds", "30",
    ]) == 0

    created = json.loads(capsys.readouterr().out)
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.get_mission(created["mission"]["id"])
    assert mission["objective"] == "verbatim\nobjective\n"
    assert mission["config"]["native_tui"] is True
    assert mission["config"]["verifier"]["argv"] == ["python", "-c", "import json; print(json.dumps({'passed': True}))"]
    assert mission["config"]["verifier"]["timeout_seconds"] == 17
    assert mission["config"]["capacity_refresh_seconds"] == 30


def test_mission_freezes_provider_model_and_route_config(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    config_path = tmp_path / "config.toml"
    config_text = '[providers.codex]\nmodel = "spark"\n[ralph]\nroutes = ["codex"]\n'
    config_path.write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(config_path))

    assert main(["mission", "create", "--prompt", "objective", "--cwd", str(tmp_path), "--json"]) == 0

    created = json.loads(capsys.readouterr().out)
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.get_mission(created["mission"]["id"])
    config_path.write_text("[providers.codex]\nmodel = \"changed\"\n", encoding="utf-8")
    assert mission["config"]["frozen_tool_config"] == config_text


def test_resume_wakes_active_controller_without_reconciling_live_operation(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path, config={"headless": True})
    task_id = store.add_task(mission["id"], "active task")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id, process_pid=12345, process_start="start-token")
    lock = MissionLock(state / "locks", mission["id"])
    lock.acquire()
    try:
        assert main(["resume", "--mission", mission["id"]]) == 0
    finally:
        lock.release()
    capsys.readouterr()

    assert store.get_operation(operation_id)["status"] == "RUNNING"
    assert store.get_task(task_id)["status"] == "RUNNING"

