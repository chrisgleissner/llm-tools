"""Typed steering is persisted and applied only at safe Bridge boundaries."""

from __future__ import annotations

import json
from pathlib import Path

from llm_tools import bridge
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore


def _mission(store: BridgeStore, tmp_path: Path, *, verifier: list[str] | None = None):
    config = {"providers": "codex,claude", "routes": ""}
    if verifier:
        config["verifier"] = {"argv": verifier, "timeout_seconds": 3}
    mission = store.create_mission("mission", "objective", cwd=tmp_path, config=config)
    task = store.add_task(mission["id"], "original task", title="Task")
    store.set_mission_status(mission["id"], "RUNNING")
    return mission, task


def test_replace_is_revisioned_and_preserves_original_objective(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission, task_id = _mission(store, tmp_path)
        result = bridge._apply_operator_command(store, mission["id"], {
            "action": "steer", "kind": "replace", "task_id": task_id, "prompt": "Revised remaining work",
        })
        task = store.get_task(task_id)

        assert result["result"]["status"] == "APPLIED"  # nothing runs the task, so there is no boundary to wait for
        assert task["original_prompt"] == "original task"
        assert task["revision"] == 2
        assert task["prompt"] == "Revised remaining work"
        assert task["pending"] == {}


def test_steer_cli_accepts_verbatim_prompt_file(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    prompt_file = tmp_path / "replacement.md"
    prompt_file.write_text("Keep line one\n\nKeep trailing newline\n", encoding="utf-8")
    with bridge.open_store() as store:
        mission, task_id = _mission(store, tmp_path)

    assert bridge.main([
        "steer", "--mission", mission["id"], "--kind", "replace", "--task", str(task_id),
        "--file", str(prompt_file), "--json",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["result"]["kind"] == "replace"
    with bridge.open_store() as store:
        assert store.get_task(task_id)["prompt"] == "Keep line one\n\nKeep trailing newline\n"


def test_review_creates_dependent_operator_review_task(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission, task_id = _mission(store, tmp_path)
        result = bridge._apply_operator_command(store, mission["id"], {"action": "steer", "kind": "review", "task_id": task_id})
        review_id = int(result["result"]["task_id"])
        review = store.get_task(review_id)

        assert review["depends_on"] == [task_id]
        assert review["status"] == "QUEUED"
        assert "Review task" in review["prompt"]


def test_verify_request_runs_configured_verifier_at_safe_boundary(tmp_path: Path) -> None:
    verifier = tmp_path / "verify.py"
    verifier.write_text("import json; print(json.dumps({'passed': True, 'summary': 'verified'}))\n", encoding="utf-8")
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission, task_id = _mission(store, tmp_path, verifier=["python", str(verifier)])
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.mark_operation_running(operation)
        store.finish_operation(operation, exit_code=0)
        result = bridge._apply_operator_command(store, mission["id"], {"action": "steer", "kind": "verify", "task_id": task_id})
        hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hook.bind(store)

        hook._apply_pending_boundary_requests()

        assert result["result"]["status"] == "QUEUED_FOR_EXIT"
        assert store.get_task(task_id)["status"] == "COMPLETED"
        assert store.get_mission(mission["id"])["config"]["verification_requests"] == []


def test_pause_after_iteration_applies_after_operation_exit(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission, task_id = _mission(store, tmp_path)
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.mark_operation_running(operation)
        bridge._apply_operator_command(store, mission["id"], {"action": "steer", "kind": "pause-after-iteration"})
        hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hook.bind(store)

        hook.after_iteration({"operation_id": operation}, {"provider": "codex"}, type("Cfg", (), {"run_dir": None, "cwd": str(tmp_path), "provider": "codex"})(), 1, 0, 1.0)

        assert store.get_mission(mission["id"])["status"] == "PAUSED"
        assert store.get_task(task_id)["status"] == "VERIFYING"


def test_stop_after_task_stops_only_after_verified_completion(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission, task_id = _mission(store, tmp_path)
        bridge._apply_operator_command(store, mission["id"], {"action": "steer", "kind": "stop-after-task", "task_id": task_id})
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.mark_operation_running(operation)
        store.finish_operation(operation, exit_code=0)
        store.verify_task(task_id, evidence="artifact verified")
        hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hook.bind(store)

        context = hook.before_iteration()

        assert context["action"] == "stop"
        assert store.get_mission(mission["id"])["status"] == "STOPPING"


def test_handoff_preference_selects_requested_usable_station_once(tmp_path: Path) -> None:
    with BridgeStore(tmp_path / "bridge.sqlite3") as store:
        mission, _task_id = _mission(store, tmp_path)
        bridge._apply_operator_command(store, mission["id"], {"action": "steer", "kind": "handoff", "route": "claude"})
        hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hook.bind(store)
        selection = {
            "index": 0, "provider": "codex", "rotation_reason": "current-usable", "decisions": [
                {"provider": "codex", "usable": True}, {"provider": "claude", "usable": True},
            ],
        }

        chosen = hook.apply_handoff_preference(selection)

        assert chosen["provider"] == "claude"
        assert chosen["rotation_reason"] == "operator-handoff"
        assert store.get_mission(mission["id"])["config"]["handoff_preference"] == ""
