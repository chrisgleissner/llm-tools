"""Sending a task back (reject) and making the saved provider rotation explicit."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_format, bridge_tui, ralph_robin
from llm_tools.bridge import dispatch_console_command, main
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore, MissionLock, TransitionError, rotation_label
from tests.conftest import scaled, wait_for as _shared_wait_for

FROZEN = (
    '[providers.codex]\nmodel = "gpt-5"\n'
    '[routes.claude-sonnet]\nprovider = "claude"\nmodel = "sonnet-5"\n'
    '[routes.kilo-glm]\nprovider = "kilo"\nmodel = "zai/glm-5.2"\n'
)


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(directory))
    return directory


def _verifying_task(store: BridgeStore, mission_id: str, prompt: str = "write the thing") -> int:
    task_id = store.add_task(mission_id, prompt, title="Write the thing")
    if store.get_mission(mission_id)["status"] == "CREATED":
        store.set_mission_status(mission_id, "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation)
    store.finish_operation(operation, exit_code=0)
    assert store.get_task(task_id)["status"] == "VERIFYING"
    return task_id


def _mission(store: BridgeStore, tmp_path: Path, **config: object) -> dict:
    return store.create_mission("m", "objective", cwd=tmp_path, config={"frozen_tool_config": FROZEN, **config})


def _cli_mission(tmp_path: Path, capsys, *extra: str) -> str:
    assert main(["mission", "create", "--prompt", "Do the work", "--name", "demo", "--cwd", str(tmp_path), *extra, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["mission"]["id"]


# --- store ------------------------------------------------------------------


def test_reject_moves_verifying_to_queued_with_ordered_events(tmp_path) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path)
        task_id = _verifying_task(store, mission["id"])
        before = store.events(mission["id"])[-1]["id"]

        task = store.reject_task(task_id, "  you never ran the tests  ")

        assert task["status"] == "QUEUED"
        events = [event for event in store.events(mission["id"], after=before)]
        assert [event["kind"] for event in events] == ["task-status", "task-rejected", "task-status"]
        assert [(e["data"].get("from"), e["data"].get("to")) for e in (events[0], events[2])] == [("VERIFYING", "FAILED"), ("FAILED", "QUEUED")]
        assert events[1]["data"] == {"reason": "you never ran the tests", "origin": "operator", "revision": 1}
        assert events[1]["task_id"] == task_id
        assert store.latest_rejection_reason(mission["id"], task_id) == "you never ran the tests"
        assert [item["id"] for item in store.ready_tasks(mission["id"])] == [task_id]


@pytest.mark.parametrize("make_state", ["queued", "running", "completed"])
def test_reject_refuses_tasks_that_are_not_awaiting_verification(tmp_path, make_state) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path)
        store.set_mission_status(mission["id"], "RUNNING")
        task_id = store.add_task(mission["id"], "work")
        if make_state != "queued":
            operation = store.begin_operation(task_id, iteration=1, station="codex")
            store.mark_operation_running(operation)
            if make_state == "completed":
                store.finish_operation(operation, exit_code=0)
                store.verify_task(task_id, approved=True)
        status = store.get_task(task_id)["status"]
        last = store.events(mission["id"])[-1]["id"]

        with pytest.raises(TransitionError, match="not awaiting verification"):
            store.reject_task(task_id, "no")

        assert store.get_task(task_id)["status"] == status
        assert store.events(mission["id"], after=last) == []


def test_reject_validates_the_reason_and_leaves_the_task_untouched(tmp_path) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path)
        task_id = _verifying_task(store, mission["id"])
        for bad in ("", "   \n"):
            with pytest.raises(ValueError, match="reason is required"):
                store.reject_task(task_id, bad)
        with pytest.raises(ValueError, match="at most 2000"):
            store.reject_task(task_id, "x" * 2001)
        assert store.get_task(task_id)["status"] == "VERIFYING"
        assert store.latest_rejection_reason(mission["id"], task_id) == ""

        assert store.reject_task(task_id, "x" * 2000)["status"] == "QUEUED"


def test_reject_is_atomic_when_the_second_transition_fails(tmp_path, monkeypatch) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path)
        task_id = _verifying_task(store, mission["id"])
        last = store.events(mission["id"])[-1]["id"]
        original = store.set_task_status

        def fail_on_queue(identifier: int, status: str):
            if status == "QUEUED":
                raise TransitionError("simulated failure")
            return original(identifier, status)

        monkeypatch.setattr(store, "set_task_status", fail_on_queue)
        with pytest.raises(TransitionError, match="simulated"):
            store.reject_task(task_id, "retry it")

        monkeypatch.setattr(store, "set_task_status", original)
        assert store.get_task(task_id)["status"] == "VERIFYING"
        assert store.events(mission["id"], after=last) == []


def test_latest_rejection_reason_is_the_newest_one(tmp_path) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path)
        task_id = _verifying_task(store, mission["id"])
        store.reject_task(task_id, "first reason")
        operation = store.begin_operation(task_id, iteration=2, station="codex")
        store.mark_operation_running(operation)
        store.finish_operation(operation, exit_code=0)
        store.reject_task(task_id, "second reason")
        assert store.latest_rejection_reason(mission["id"], task_id) == "second reason"


def test_set_rotation_records_changes_once(tmp_path) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path, providers="claude,codex", routes="claude-sonnet")
        mid = mission["id"]
        assert rotation_label("claude,codex", "claude-sonnet") == "claude-sonnet"
        same = store.set_rotation(mid, providers="claude,codex", routes="claude-sonnet", source="rotation")
        assert same["changed"] is False
        assert not [e for e in store.events(mid) if e["kind"] == "rotation-changed"]

        changed = store.set_rotation(mid, providers="claude,codex", routes="codex", source="start")
        assert (changed["changed"], changed["from"], changed["to"]) == (True, "claude-sonnet", "codex")
        event = [e for e in store.events(mid) if e["kind"] == "rotation-changed"][0]
        assert event["data"] == {"from": "claude-sonnet", "to": "codex", "providers": "claude,codex", "routes": "codex", "source": "start"}
        assert store.get_mission(mid)["config"]["routes"] == "codex"

        bare = store.create_mission("bare", "objective", cwd=tmp_path)
        initial = store.set_rotation(bare["id"], providers="codex", routes="", source="start")
        assert initial["changed"] is False
        assert not [e for e in store.events(bare["id"]) if e["kind"] == "rotation-changed"]
        assert store.get_mission(bare["id"])["config"]["providers"] == "codex"


# --- CLI: task reject -------------------------------------------------------


def test_task_reject_receipt_text_and_json(tmp_path, state, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        first = _verifying_task(store, mid)
        second = _verifying_task(store, mid, "other work")
    assert main(["task", "reject", "--mission", mid, "--task", str(first), "--reason", "tests missing"]) == 0
    assert capsys.readouterr().out.strip() == f'Sent back · task {first} "Write the thing" · QUEUED · reason recorded'

    assert main(["task", "reject", "--mission", mid, "--task", str(second), "--reason", "bad", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "APPLIED"
    assert payload["result"]["status"] == "QUEUED"
    assert payload["result"]["rejection_reason"] == "bad"
    assert payload["payload"] == {"task_id": second, "action": "task.reject", "reason": "bad"}
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(first)["status"] == "QUEUED"


def test_task_reject_requires_a_reason_and_the_right_state(tmp_path, state, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys)
    with pytest.raises(SystemExit) as exit_info:
        main(["task", "reject", "--mission", mid, "--task", "1"])
    assert exit_info.value.code == 2
    assert "--reason" in capsys.readouterr().err

    assert main(["task", "reject", "--mission", mid, "--task", "1", "--reason", "nope"]) == 2
    assert "not awaiting verification" in capsys.readouterr().err
    assert main(["task", "reject", "--mission", mid, "--task", "1", "--reason", "x" * 2001]) == 2
    assert "at most 2000" in capsys.readouterr().err


# --- console alias ----------------------------------------------------------


def test_console_reject_alias_with_explicit_and_implicit_task(tmp_path, state, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys)
    with BridgeStore(state / "bridge.sqlite3") as store:
        first = _verifying_task(store, mid)
        second = _verifying_task(store, mid, "second")

    ambiguous = dispatch_console_command(mid, "/reject needs more tests")
    assert ambiguous == f"error: reject needs a task number · tasks awaiting approval: {first}, {second}"

    explicit = json.loads(dispatch_console_command(mid, f"/reject {first} -vv output was missing"))
    assert explicit["status"] == "APPLIED" and explicit["result"]["status"] == "QUEUED"
    assert explicit["result"]["rejection_reason"] == "-vv output was missing"

    implicit = json.loads(dispatch_console_command(mid, "reject please add a changelog entry"))
    assert implicit["result"]["id"] == second
    assert implicit["result"]["rejection_reason"] == "please add a changelog entry"

    assert dispatch_console_command(mid, "reject") == "error: usage: reject [TASK-NUMBER] REASON"
    assert dispatch_console_command(mid, f"reject {first}") == "error: usage: reject [TASK-NUMBER] REASON"
    assert dispatch_console_command(mid, "reject because") == "error: reject needs a task number · no task awaits approval"
    assert dispatch_console_command(mid, f"reject {first} again").startswith("error: ")


def test_tui_reject_alias_expands_and_asks_for_confirmation() -> None:
    snapshot = {"tasks": [{"id": 4, "ordinal": 4, "status": "VERIFYING"}, {"id": 5, "ordinal": 5, "status": "QUEUED"}]}
    command, problem = bridge_tui._expand_console_alias("/reject missing tests", snapshot)
    assert problem is None
    assert shlex.split(command) == ["task", "reject", "--task", "4", "--reason=missing tests"]
    reason = bridge_tui._confirmation_reason(command)
    assert reason == "Send task 4 back for another attempt? Reason: missing tests"

    explicit, _ = bridge_tui._expand_console_alias("reject 9 do it again", snapshot)
    assert shlex.split(explicit)[:4] == ["task", "reject", "--task", "9"]
    assert bridge_tui._expand_console_alias("reject", snapshot)[1] == "error: usage: reject [TASK-NUMBER] REASON"
    assert "no task awaits approval" in bridge_tui._expand_console_alias("reject why", {"tasks": []})[1]
    literal, literal_problem = bridge_tui._expand_console_alias("reject 'unterminated", snapshot)
    assert literal_problem is None and shlex.split(literal)[-1] == "--reason='unterminated"  # prose is taken literally
    assert "reject" in bridge_tui._CONSOLE_ALIASES


def test_tui_describes_rejection_and_rotation_events() -> None:
    sent_back = bridge_tui._event_summary({"kind": "task-rejected", "task_id": 7, "data": {"reason": "tests missing"}})
    assert sent_back == "task 7 sent back · tests missing"
    rotated = bridge_tui._event_summary({"kind": "rotation-changed", "data": {"from": "claude-sonnet", "to": "codex"}})
    assert rotated == "rotation claude-sonnet → codex"


# --- managed context --------------------------------------------------------


def test_next_attempt_context_quotes_the_rejection_reason(tmp_path) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path)
        task_id = _verifying_task(store, mission["id"], "build the parser")
        hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hooks.bind(store)
        store.reject_task(task_id, "The parser ignores comments.\nFix that first.")

        context = hooks.before_iteration()

        assert context["action"] == "run" and context["task_id"] == task_id
        prompt = context["prompt"]
        assert "Previous attempt was rejected by the operator: The parser ignores comments.\nFix that first." in prompt
        assert prompt.index("Previous attempt was rejected") < prompt.index("Durable checkpoint")
        assert "task-rejected" in prompt  # the checkpoint history still lists the rejection event


def test_context_has_no_rejection_text_for_a_fresh_task(tmp_path) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path)
        store.set_mission_status(mission["id"], "RUNNING")
        store.add_task(mission["id"], "fresh work")
        hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hooks.bind(store)
        assert "rejected by the operator" not in hooks.before_iteration()["prompt"]


# --- rotation command -------------------------------------------------------


def test_rotation_shows_saved_order_cli_model_and_selection(tmp_path, state, monkeypatch, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--routes", "claude-sonnet,codex")
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.get_mission(mid)
        store.set_mission_config_value(mid, "frozen_tool_config", FROZEN, event_kind="test-config")
        store.record_runtime_event(mid, "provider-selection", {"route": "claude-sonnet", "provider": "claude"})
        assert mission["config"]["routes"] == "claude-sonnet,codex"
    monkeypatch.setattr(bridge, "controller_running", lambda _mission: False)

    assert main(["rotation", "--mission", mid]) == 0
    text = capsys.readouterr().out
    lines = text.splitlines()
    assert lines[0] == f"Rotation for mission {mid[:8]} · routes mode · tried in this order"
    assert lines[1].split() == ["#", "NAME", "LAUNCH", "CLI", "MODEL", "NOW"]
    assert lines[2].split() == ["1", "claude-sonnet", "claude", "sonnet-5", "selected"]
    assert lines[3].split() == ["2", "codex", "codex", "gpt-5"]
    assert lines[-1] == f"Change it with: llm-bridge rotation --mission {mid[:8]} --set LIST"

    assert main(["rotation", "--mission", mid, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "routes" and data["selected"] == "claude-sonnet" and data["updated"] is False
    assert data["entries"][1] == {"position": 2, "name": "codex", "launch_cli": "codex", "model": "gpt-5", "selected": False}


def test_rotation_in_providers_mode_uses_provider_models(tmp_path, state, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--providers", "codex,claude")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_config_value(mid, "frozen_tool_config", FROZEN, event_kind="test-config")
    assert main(["rotation", "--mission", mid, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "providers" and data["selected"] is None
    assert [(e["name"], e["launch_cli"], e["model"]) for e in data["entries"]] == [("codex", "codex", "gpt-5"), ("claude", "claude", None)]
    assert main(["rotation", "--mission", mid]) == 0
    assert "provider default" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("option", "value", "providers", "routes"),
    [
        ("--set", "claude-sonnet,codex", "claude,codex", "claude-sonnet,codex"),
        ("--set", "codex,claude", "codex,claude", ""),
        ("--routes", "kilo-glm", "claude,codex", "kilo-glm"),
        ("--providers", "codex", "codex", ""),
    ],
)
def test_rotation_change_saves_and_announces(tmp_path, state, monkeypatch, capsys, option, value, providers, routes) -> None:
    mid = _cli_mission(tmp_path, capsys, "--providers", "claude,codex", "--routes", "claude-sonnet")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_config_value(mid, "frozen_tool_config", FROZEN, event_kind="test-config")
    monkeypatch.setattr(bridge, "controller_running", lambda _mission: True)

    assert main(["rotation", "--mission", mid, option, value]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Rotation changed: claude-sonnet → {(routes or providers).replace(',', ', ')} (saved for this mission)"
    assert lines[1] == "Applies from the next provider selection; a provider that is running now is not interrupted."
    with BridgeStore(state / "bridge.sqlite3") as store:
        config = store.get_mission(mid)["config"]
        assert (config["providers"], config["routes"]) == (providers, routes)
        event = [e for e in store.events(mid) if e["kind"] == "rotation-changed"][-1]
        assert event["data"]["from"] == "claude-sonnet" and event["data"]["source"] == "rotation"


def test_rotation_change_when_stopped_and_when_unchanged(tmp_path, state, monkeypatch, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--providers", "claude")
    monkeypatch.setattr(bridge, "controller_running", lambda _mission: False)
    assert main(["rotation", "--mission", mid, "--set", "codex"]) == 0
    assert "Takes effect the next time the mission starts." in capsys.readouterr().out
    assert main(["rotation", "--mission", mid, "--set", "codex"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Rotation unchanged: codex (already saved for this mission)"
    assert main(["rotation", "--mission", mid, "--set", "kilo", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["changed"], data["from"], data["to"], data["updated"]) == (True, "codex", "kilo", True)


def test_rotation_rejects_unknown_names_with_suggestions(tmp_path, state, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--providers", "claude")
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_config_value(mid, "frozen_tool_config", FROZEN, event_kind="test-config")
    assert main(["rotation", "--mission", mid, "--set", "claude-sonet"]) == 2
    err = capsys.readouterr().err
    assert "unknown rotation entry 'claude-sonet' · did you mean claude-sonnet" in err
    assert "available: claude-sonnet, kilo-glm" in err
    assert main(["rotation", "--mission", mid, "--providers", "claude-sonnet"]) == 2
    assert "unknown rotation entry 'claude-sonnet'" in capsys.readouterr().err
    assert main(["rotation", "--mission", mid, "--routes", " , "]) == 2
    assert "at least one" in capsys.readouterr().err
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["config"]["providers"] == "claude"
    with pytest.raises(SystemExit):
        main(["rotation", "--mission", mid, "--set", "a", "--routes", "b"])


def test_rotation_console_command_and_help(tmp_path, state, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--providers", "claude")
    shown = json.loads(dispatch_console_command(mid, "/rotation --json"))
    assert shown["entries"][0]["name"] == "claude"
    changed = json.loads(dispatch_console_command(mid, "rotation --set codex --json"))
    assert changed["changed"] is True and changed["to"] == "codex"
    assert dispatch_console_command(mid, "rotation --set nonsense").startswith("error: ")

    with pytest.raises(SystemExit):
        main(["rotation", "--help"])
    assert "applies from the next provider selection" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["start", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert help_text.count("changes the mission's saved rotation") == 2


def test_status_shows_the_saved_rotation(tmp_path, state, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--routes", "claude-sonnet,codex")
    assert main(["status", "--mission", mid]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "Rotation    claude-sonnet → codex" in lines
    assert lines.index("Rotation    claude-sonnet → codex") < lines.index(next(line for line in lines if line.startswith("Current")))
    assert "Rotation" not in bridge_format.format_status({"mission": {"id": "x" * 8, "name": "n", "status": "CREATED", "config": {}}})


# --- start / persistence ----------------------------------------------------


def test_start_override_is_saved_announced_and_reused(tmp_path, state, monkeypatch, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--routes", "claude-sonnet")
    launches: list[dict] = []
    monkeypatch.setattr(bridge, "start_mission", lambda mission_id, **kwargs: launches.append(kwargs) or 0)
    monkeypatch.setattr(bridge, "controller_running", lambda _mission: False)

    assert main(["start", "--mission", mid, "--routes", "codex"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Rotation changed: claude-sonnet → codex (saved for this mission)"]
    assert launches[-1]["routes"] == "codex"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["config"]["routes"] == "codex"
        event = [e for e in store.events(mid) if e["kind"] == "rotation-changed"][0]
        assert (event["data"]["from"], event["data"]["to"], event["data"]["source"]) == ("claude-sonnet", "codex", "start")

    # A later plain start uses the saved rotation, which is now Codex, and says nothing.
    assert main(["start", "--mission", mid]) == 0
    assert capsys.readouterr().out == ""
    assert launches[-1]["routes"] == "codex"

    # Naming the saved rotation again is not a change.
    assert main(["start", "--mission", mid, "--routes", "codex"]) == 0
    assert capsys.readouterr().out == ""

    # A providers-only override clears the saved routes and is announced.
    assert main(["start", "--mission", mid, "--providers", "claude"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Rotation changed: codex → claude (saved for this mission)"]
    assert launches[-1]["routes"] == "" and launches[-1]["providers"] == "claude"


def test_start_override_is_refused_while_a_controller_owns_the_mission(tmp_path, state, monkeypatch, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--routes", "claude-sonnet")
    started: list[object] = []
    monkeypatch.setattr(bridge, "start_mission", lambda *args, **kwargs: started.append(args) or 0)
    monkeypatch.setattr(bridge, "controller_running", lambda _mission: True)

    assert main(["start", "--mission", mid, "--routes", "codex"]) == 2
    assert f"llm-bridge rotation --mission {mid[:8]} --set LIST" in capsys.readouterr().err
    assert started == []
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mid)["config"]["routes"] == "claude-sonnet"
    # Without a rotation change, start behaves as before.
    assert main(["start", "--mission", mid]) == 0


def test_console_start_reports_the_rotation_change(tmp_path, state, monkeypatch, capsys) -> None:
    mid = _cli_mission(tmp_path, capsys, "--routes", "claude-sonnet", "--headless")

    def fake_run(argv, **_kwargs):
        with BridgeStore(state / "bridge.sqlite3") as store:
            store.set_rotation(mid, providers="claude,codex", routes="codex", source="start")
            store.set_mission_status(mid, "RUNNING")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    response = json.loads(dispatch_console_command(mid, "/start --routes codex"))
    assert response["result"]["detail"].startswith("Rotation changed: claude-sonnet → codex (saved for this mission) · Running")


def test_run_controller_persists_override_through_set_rotation(tmp_path, state, monkeypatch) -> None:
    from llm_tools import bridge_runtime

    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("m", "objective", cwd=tmp_path, config={"providers": "claude,codex", "routes": "claude-sonnet", "headless": True})
    mid = mission["id"]
    seen: dict[str, object] = {}

    def fake_main(argv, bridge_hooks=None):
        seen["argv"] = argv
        seen["applied"] = bridge_hooks.applied_rotation
        return 0

    monkeypatch.setattr(bridge_runtime.ralph_robin, "main", fake_main)
    monkeypatch.setattr(bridge_runtime, "CapacityRefreshWorker", lambda *a, **k: type("W", (), {"start": lambda s: None, "stop": lambda s: None})())
    bridge_runtime.run_controller(mid, headless=True, providers="claude,codex", routes="codex")

    assert seen["applied"] == ("claude,codex", "codex")
    assert "codex" in seen["argv"]
    with BridgeStore(state / "bridge.sqlite3") as store:
        kinds = [e["kind"] for e in store.events(mid)]
        assert "rotation-changed" in kinds and "controller-config" in kinds
        assert store.get_mission(mid)["config"]["routes"] == "codex"


def test_hooks_report_a_changed_rotation_once(tmp_path) -> None:
    with BridgeStore(tmp_path / "b.sqlite3") as store:
        mission = _mission(store, tmp_path, providers="claude,codex", routes="claude-sonnet")
        hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
        hooks.bind(store)
        assert hooks.rotation_update() is None  # nothing applied yet: no baseline
        hooks.applied_rotation = ("claude,codex", "claude-sonnet")
        assert hooks.rotation_update() is None
        store.set_rotation(mission["id"], providers="claude,codex", routes="codex", source="rotation")
        pending = hooks.rotation_update()
        assert pending == ("claude,codex", "codex")
        hooks.rotation_applied(pending, "")
        assert hooks.rotation_update() is None
        hooks.rotation_applied(("x", "y"), "bad")
        kinds = [e["kind"] for e in store.events(mission["id"])]
        assert kinds.count("rotation-applied") == 1 and kinds.count("rotation-rejected") == 1


def test_ralph_reload_rotation_validates_and_restores(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_TOOLS_CONFIG", str(tmp_path / "missing.toml"))
    cfg = ralph_robin.RalphConfig()
    cfg.prompt_text = "work"
    cfg.providers_spec = "claude"
    ralph_robin.validate_args(cfg)
    ralph_robin.resolve_policies(cfg, {})

    assert ralph_robin.reload_rotation(cfg, "codex,claude", "") == ""
    assert cfg.providers == ["codex", "claude"] and cfg.routes == []

    assert ralph_robin.reload_rotation(cfg, "codex", "codex") == ""
    assert cfg.routes == ["codex"] and cfg.route_policies["codex"].provider == "codex"

    assert "not valid" in ralph_robin.reload_rotation(cfg, "codex", "no-such-route")
    assert cfg.routes == ["codex"] and cfg.routes_spec == "codex" and cfg.providers_spec == "codex"


# --- end to end with a fake provider ----------------------------------------


def _call(env, *args):
    result = subprocess.run([sys.executable, "-m", "llm_tools.bridge", *map(str, args)], env=env, text=True, capture_output=True, timeout=scaled(30))
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def _wait(predicate, timeout=20):
    return _shared_wait_for(predicate, timeout, 0.05, message="expected state was not reached")


def _released(state, mission_id) -> bool:
    lock = MissionLock(state / "locks", mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        return False
    lock.release()
    return True


def test_reject_reruns_the_task_with_the_reason_and_a_changed_rotation(tmp_path, env) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    ready = {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": ready, "claude": ready})
    fake = tmp_path / "provider.py"
    fake.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "prompt = Path(sys.argv[2]).read_text()\n"
        "with Path('received.jsonl').open('a') as out:\n"
        "    out.write(json.dumps({'provider': sys.argv[1], 'prompt': prompt}) + '\\n')\n"
        "print('provider finished', flush=True)\n"
    )
    template = shlex.join([sys.executable, str(fake), "{provider}", "{prompt_file}"])
    created = json.loads(_call(env, "mission", "create", "--prompt", "Write the parser", "--cwd", tmp_path, "--headless",
                               "--providers", "codex", "--command-template", template, "--json"))
    mid, task = created["mission"]["id"], created["task"]["id"]
    database = state / "bridge.sqlite3"

    def task_status() -> str:
        with BridgeStore(database) as store:
            return store.get_task(task)["status"]

    def received() -> list[dict]:
        path = tmp_path / "received.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    try:
        _call(env, "start", "--mission", mid, "--headless")
        _wait(lambda: task_status() == "VERIFYING")
        assert [item["provider"] for item in received()] == ["codex"]
        assert "rejected by the operator" not in received()[0]["prompt"]

        # Change the rotation while the controller is live, then send the task back.
        out = _call(env, "rotation", "--mission", mid, "--set", "claude")
        assert out.splitlines()[0] == "Rotation changed: codex → claude (saved for this mission)"
        assert "Applies from the next provider selection" in out.splitlines()[1]
        out = _call(env, "task", "reject", "--mission", mid, "--task", task, "--reason", "The parser ignores comments")
        assert out.strip() == 'Sent back · task 1 "Write the parser" · QUEUED · reason recorded'

        _wait(lambda: len(received()) == 2 and task_status() == "VERIFYING")
        second = received()[1]
        assert second["provider"] == "claude"  # the changed rotation applied at the next selection
        assert "Previous attempt was rejected by the operator: The parser ignores comments" in second["prompt"]
        with BridgeStore(database) as store:
            kinds = [event["kind"] for event in store.events(mid)]
            assert "task-rejected" in kinds and "rotation-changed" in kinds and "rotation-applied" in kinds

        _call(env, "task", "verify", "--mission", mid, "--task", task, "--approve")
        _wait(lambda: task_status() == "COMPLETED")
        assert len(received()) == 2
    finally:
        with BridgeStore(database) as store:
            if store.get_mission(mid)["status"] not in {"COMPLETED", "STOPPED", "CANCELLED", "FAILED"}:
                _call(env, "stop", "--mission", mid)
        _wait(lambda: _released(state, mid))
