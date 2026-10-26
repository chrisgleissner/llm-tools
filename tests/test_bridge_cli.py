"""Public LLM Bridge CLI contracts."""

from __future__ import annotations

import json
import io
import shlex
from pathlib import Path
import pytest

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

    assert main(["mission", "list", "--json"]) == 0
    assert [item["id"] for item in json.loads(capsys.readouterr().out)["missions"]] == [mission_id]
    assert main(["doctor", "--mission", mission_id, "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["sqlite_integrity"] == "ok" and doctor["mission"]["id"] == mission_id
    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mission"] is None


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


def test_proposals_are_durable_and_require_explicit_operator_acceptance(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("proposals", "objective", cwd=tmp_path)
        source_task = store.add_task(mission["id"], "source work")
        store.set_mission_status(mission["id"], "RUNNING")
        source_operation = store.begin_operation(source_task, iteration=1, station="codex")
        store.finish_operation(source_operation, exit_code=0)

    assert main([
        "proposal", "create", "--mission", mission["id"], "--operation", source_operation,
        "--prompt", "Add regression coverage", "--title", "Regression coverage", "--priority", "40", "--json",
    ]) == 0
    proposed = json.loads(capsys.readouterr().out)
    task_id = proposed["result"]["id"]
    assert proposed["status"] == "APPLIED"
    assert proposed["result"]["origin"] == "agent-proposal"
    assert proposed["result"]["proposal_status"] == "PENDING"
    assert proposed["result"]["proposed_by_operation"] == source_operation

    assert main(["proposal", "list", "--mission", mission["id"], "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [task["id"] for task in listing["proposals"]] == [task_id]
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert task_id not in {task["id"] for task in store.ready_tasks(mission["id"])}

    assert main(["proposal", "accept", "--mission", mission["id"], "--task", str(task_id), "--json"]) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "APPLIED"
    assert accepted["result"]["proposal_status"] == "ACCEPTED"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert task_id in {task["id"] for task in store.ready_tasks(mission["id"])}

    assert main([
        "proposal", "create", "--mission", mission["id"], "--operation", source_operation,
        "--prompt", "Out of scope", "--json",
    ]) == 0
    rejected_task_id = json.loads(capsys.readouterr().out)["result"]["id"]
    assert main([
        "proposal", "reject", "--mission", mission["id"], "--task", str(rejected_task_id),
        "--reason", "not needed", "--json",
    ]) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["result"]["proposal_status"] == "REJECTED"
    assert rejected["result"]["status"] == "CANCELLED"


def test_proposal_cli_rejects_foreign_operation_provenance(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        first = store.create_mission("first", "objective", cwd=tmp_path)
        second_cwd = tmp_path / "other"
        second_cwd.mkdir()
        second = store.create_mission("second", "objective", cwd=second_cwd)
        task = store.add_task(first["id"], "first task")
        store.set_mission_status(first["id"], "RUNNING")
        operation = store.begin_operation(task, iteration=1, station="codex")

    assert main([
        "proposal", "create", "--mission", second["id"], "--operation", operation,
        "--prompt", "must not cross mission", "--json",
    ]) == 2
    assert "same mission" in capsys.readouterr().err
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.list_proposals(second["id"]) == []


def test_console_can_review_and_accept_proposals_with_quoted_reason(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("console proposal", "objective", cwd=tmp_path)
        source_task = store.add_task(mission["id"], "source")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(source_task, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
        proposal_id = store.propose_task(mission["id"], "follow-up", source_operation_id=operation)

    listed = json.loads(dispatch_console_command(mission["id"], "proposals"))
    assert [item["id"] for item in listed["proposals"]] == [proposal_id]
    result = json.loads(dispatch_console_command(mission["id"], f"proposal reject {proposal_id} outside current scope"))
    assert result["status"] == "APPLIED"
    assert result["result"]["proposal_status"] == "REJECTED"
    assert result["result"]["status"] == "CANCELLED"

    invalid = dispatch_console_command(mission["id"], f"proposal accept {proposal_id} extra")
    assert "usage:" in invalid


def test_console_preserves_public_proposal_option_grammar(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("console public grammar", "objective", cwd=tmp_path)
        source_task = store.add_task(mission["id"], "source")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(source_task, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
        proposal_id = store.propose_task(mission["id"], "follow-up", source_operation_id=operation)

    response = json.loads(dispatch_console_command(
        mission["id"], f"proposal reject --task {proposal_id} --reason 'scope changed'"
    ))
    assert response["status"] == "APPLIED"
    assert response["result"]["proposal_status"] == "REJECTED"


def test_console_reuses_cli_task_grammar_for_multiline_add_and_amend(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    objective_file = tmp_path / "new-task.md"
    objective_file.write_text("First pasted line\nSecond pasted line\n", encoding="utf-8")
    amendment_file = tmp_path / "amendment.md"
    amendment_file.write_text("Revised first line\nKeep full multiline text\n", encoding="utf-8")
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("repl", "objective", cwd=tmp_path)
        dependency = store.add_task(mission["id"], "dependency task")
        store.set_mission_status(mission["id"], "RUNNING")

    response = json.loads(dispatch_console_command(
        mission["id"],
        f"task add --priority 5 --acceptance 'tests pass' --depends-on {dependency} --file {json.dumps(str(objective_file))}",
    ))
    task_id = int(response["id"])
    with BridgeStore(state / "bridge.sqlite3") as store:
        task = store.get_task(task_id)
        assert task["prompt"] == objective_file.read_text(encoding="utf-8")
        assert task["priority"] == 5
        assert task["acceptance"] == ["tests pass"]
        assert task["depends_on"] == [dependency]

    amend = json.loads(dispatch_console_command(
        mission["id"], f"task amend --task {task_id} --file {json.dumps(str(amendment_file))}",
    ))
    assert amend["status"] == "APPLIED"
    assert amend["result"]["revision"] == 2
    with BridgeStore(state / "bridge.sqlite3") as store:
        task = store.get_task(task_id)
        assert task["prompt"] == objective_file.read_text(encoding="utf-8")
        assert task["pending"] == {"prompt": amendment_file.read_text(encoding="utf-8")}


def test_console_pause_resume_id_and_help_match_task_cli(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("repl", "objective", cwd=tmp_path)
        task_id = store.add_task(mission["id"], "work")
        store.set_mission_status(mission["id"], "RUNNING")

    assert "task add" in dispatch_console_command(mission["id"], "help")
    paused = json.loads(dispatch_console_command(mission["id"], f"pause {task_id}"))
    assert paused["status"] == "APPLIED"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission["id"])["status"] == "RUNNING"
        assert store.get_task(task_id)["status"] == "PAUSED"
    resumed = json.loads(dispatch_console_command(mission["id"], f"resume {task_id}"))
    assert resumed["status"] == "APPLIED"
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(task_id)["status"] in {"QUEUED", "READY"}


def test_terminal_mission_task_add_and_proposal_accept_are_reported_rejected(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("terminal", "objective", cwd=tmp_path)
        source = store.add_task(mission["id"], "verified source")
        store.set_mission_status(mission["id"], "RUNNING")
        operation = store.begin_operation(source, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
        store.verify_task(source, evidence="verified")
        proposal = store.propose_task(mission["id"], "future work", source_operation_id=operation)
        store.set_mission_status(mission["id"], "COMPLETED")

    assert main(["task", "add", "--mission", mission["id"], "--prompt", "stranded", "--json"]) == 2
    assert "terminal mission" in capsys.readouterr().err
    assert main(["proposal", "accept", "--mission", mission["id"], "--task", str(proposal), "--json"]) == 2
    assert "terminal mission" in capsys.readouterr().err
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_task(proposal)["proposal_status"] == "PENDING"
        assert len(store.list_tasks(mission["id"])) == 2


def test_cli_typed_steering_branches_persist_boundary_requests(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission(
            "steering", "objective", cwd=tmp_path,
            config={"providers": "codex,claude", "verifier": {"argv": ["true"], "timeout": 10}},
        )
        mission_id = mission["id"]
        task_id = store.add_task(mission_id, "original implementation")
        store.set_mission_status(mission_id, "RUNNING")

    def invoke(*args: str) -> dict[str, object]:
        assert main([*args, "--mission", mission_id, "--json"]) == 0
        return json.loads(capsys.readouterr().out)

    replacement = invoke("steer", "--kind", "replace", "--task", str(task_id), "--prompt", "Implement the new API")
    assert replacement["result"]["status"] == "QUEUED_FOR_BOUNDARY"
    review = invoke("steer", "--kind", "review", "--task", str(task_id))
    assert review["result"]["depends_on"] == task_id
    pause = invoke("steer", "--kind", "pause-after-iteration")
    assert pause["result"]["status"] == "QUEUED_FOR_BOUNDARY"
    stop = invoke("steer", "--kind", "stop-after-task", "--task", str(task_id))
    assert stop["result"]["status"] == "QUEUED_FOR_VERIFICATION"
    handoff = invoke("steer", "--kind", "handoff", "--route", "claude")
    assert handoff["result"]["route"] == "claude"
    with BridgeStore(state / "bridge.sqlite3") as store:
        operation = store.begin_operation(task_id, iteration=1, station="codex")
    verify = invoke("steer", "--kind", "verify", "--task", str(task_id))
    assert verify["result"]["status"] == "QUEUED_FOR_EXIT"
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_row = store.get_mission(mission_id)
        task = store.get_task(task_id)
        assert mission_row["config"]["pause_after_iteration"] is True
        assert mission_row["config"]["stop_after_task"] == task_id
        assert mission_row["config"]["handoff_preference"] == "claude"
        # The next iteration boundary applies the queued replacement.
        assert task["prompt"] == "Implement the new API"
        assert task["pending"] == {}
        assert task["status"] == "RUNNING"
        store.finish_operation(operation, exit_code=0)


def test_cli_station_view_and_invalid_steering_report_rejection(tmp_path: Path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("station", "objective", cwd=tmp_path, config={"providers": "codex"})
    assert main(["station", "--mission", mission["id"], "codex", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["config"]["viewed_station"] == "codex"
    assert main(["station", "--mission", mission["id"], "claude", "--json"]) == 2
    assert "not configured" in capsys.readouterr().err
    assert main(["steer", "--mission", mission["id"], "--kind", "handoff", "--route", "missing", "--json"]) == 2
    assert "not configured" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--log-max-bytes", "1"], "log max bytes"),
        (["--log-rotations", "21"], "log rotations"),
        (["--log-retained-runs", "0"], "retained run count"),
        (["--capacity-refresh-seconds", "4"], "capacity refresh seconds"),
        (["--verifier-arg", "true", "--verifier-timeout", "4000"], "verifier timeout"),
        (["--verifier-arg", "\x00"], "NUL byte"),
    ],
)
def test_mission_create_rejects_invalid_retention_and_verifier_options(tmp_path, monkeypatch, capsys, extra, message) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    assert main(["mission", "create", "--prompt", "objective", "--cwd", str(tmp_path), *extra]) == 2
    assert message in capsys.readouterr().err
    if (state / "bridge.sqlite3").exists():
        with BridgeStore(state / "bridge.sqlite3") as store:
            assert store.list_missions() == []


def test_legacy_mode_starts_after_creating_durable_mission_and_missing_file_is_reported(tmp_path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    started: list[tuple[object, ...]] = []
    monkeypatch.setattr("llm_tools.bridge.start_mission", lambda *args, **kwargs: started.append((args, kwargs)) or 17)
    assert main(["--prompt", "legacy objective", "--cwd", str(tmp_path)]) == 17
    assert started and started[0][0]
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.list_missions()[0]["objective"] == "legacy objective"
    assert main(["--prompt-file", str(tmp_path / "missing.md"), "--cwd", str(tmp_path)]) == 2
    assert "No such file" in capsys.readouterr().err


def test_console_legacy_aliases_share_public_task_and_event_handlers(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    prompt_file = tmp_path / "additional.md"
    prompt_file.write_text("new task from file\n", encoding="utf-8")
    amendment_file = tmp_path / "amendment.md"
    amendment_file.write_text("updated task prompt\n", encoding="utf-8")
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("console aliases", "objective", cwd=tmp_path)
        mission_id = mission["id"]
        first = store.add_task(mission_id, "first task")
        second = store.add_task(mission_id, "second task")
        store.set_mission_status(mission_id, "RUNNING")

    assert [task["id"] for task in json.loads(dispatch_console_command(mission_id, "tasks"))] == [first, second]
    assert [task["id"] for task in json.loads(dispatch_console_command(mission_id, "orders"))] == [first, second]
    assert json.loads(dispatch_console_command(mission_id, f"priority {second} 7"))["result"]["priority"] == 7
    assert json.loads(dispatch_console_command(mission_id, f"reorder {second} -4"))["result"]["ordering"] == -4
    assert json.loads(dispatch_console_command(mission_id, f"dependencies {second} {first}"))["result"]["depends_on"] == [first]
    added = json.loads(dispatch_console_command(mission_id, f"add {shlex.quote(str(prompt_file))}"))
    added_id = added["id"]
    assert added["prompt"] == "new task from file\n"
    amended = json.loads(dispatch_console_command(mission_id, f"amend {added_id} {shlex.quote(str(amendment_file))}"))
    assert amended["result"]["revision"] == 2
    with BridgeStore(state / "bridge.sqlite3") as store:
        failed_operation = store.begin_operation(added_id, iteration=1, station="fixture")
        store.finish_operation(failed_operation, exit_code=75)
    assert json.loads(dispatch_console_command(mission_id, f"retry {added_id}"))["result"]["status"] in {"QUEUED", "READY"}
    assert json.loads(dispatch_console_command(mission_id, f"pause {added_id}"))["result"]["status"] == "PAUSED"
    assert json.loads(dispatch_console_command(mission_id, f"resume {added_id}"))["result"]["status"] in {"QUEUED", "READY"}
    assert json.loads(dispatch_console_command(mission_id, f"cancel {added_id}"))["result"]["status"] == "CANCELLED"
    assert json.loads(dispatch_console_command(mission_id, "events 0"))["events"]
    assert json.loads(dispatch_console_command(mission_id, "proposals PENDING"))["proposals"] == []
    assert "terminal's native attach" in dispatch_console_command(mission_id, "attach")
    assert "Control view" in dispatch_console_command(mission_id, "control")
    assert "cannot recursively start" in dispatch_console_command(mission_id, "console")
    assert "cannot recursively start" in dispatch_console_command(mission_id, "_controller")
    assert "needs --file PATH" in dispatch_console_command(mission_id, "task add")
    assert "scoped to the active mission" in dispatch_console_command(mission_id, "task list --mission another")
    assert "error:" in dispatch_console_command(mission_id, "task list --mission")
    assert "error:" in dispatch_console_command(mission_id, "unknown-command")

    with BridgeStore(state / "bridge.sqlite3") as store:
        operation = store.begin_operation(first, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
    proposal_file = tmp_path / "follow-up.md"
    proposal_file.write_text("review follow-up", encoding="utf-8")
    created = json.loads(dispatch_console_command(mission_id, f"propose {operation} {shlex.quote(str(proposal_file))}"))
    proposal_id = created["result"]["id"]
    assert json.loads(dispatch_console_command(mission_id, f"proposal accept --task {proposal_id}"))["status"] == "APPLIED"
    assert json.loads(dispatch_console_command(mission_id, f"verify {first} --approve"))["result"]["status"] == "COMPLETED"

