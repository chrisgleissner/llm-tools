"""Typed command layer: validation, envelopes, idempotency and CLI parity."""

from __future__ import annotations

import json

import pytest

from llm_tools import bridge, bridge_commands as commands
from llm_tools.bridge import main
from llm_tools.bridge_commands import CommandFailure, CommandRequest
from llm_tools.bridge_store import BridgeStore, TransitionError


@pytest.fixture
def mission(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    assert main(["mission", "create", "--prompt", "Objective\n", "--cwd", str(tmp_path), "--providers", "codex", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    return state, created["mission"]["id"]


def _commands(state):
    with BridgeStore(state / "bridge.sqlite3") as store:
        return [dict(row) for row in store.db.execute("SELECT id,status FROM commands ORDER BY rowid")]


def test_catalog_lists_every_operation_with_typed_parameters():
    catalog = {entry["operation"]: entry for entry in commands.operation_catalog()}
    assert set(catalog) == set(commands.OPERATIONS)
    assert catalog["mission.events"]["params"] == {"after": {"type": "int", "required": False}, "limit": {"type": "int", "required": False}}
    assert catalog["mission.pause"]["mutating"] is True and catalog["mission.list"]["mutating"] is False


@pytest.mark.parametrize(
    ("request_", "code"),
    [
        (CommandRequest("mission.nope"), "unsupported-operation"),
        (CommandRequest("mission.list", {"bogus": 1}), "invalid-argument"),
        (CommandRequest("mission.list", {"include_config": "yes"}), "invalid-argument"),
        (CommandRequest("mission.events", {"limit": True}, mission_id="x"), "invalid-argument"),
        (CommandRequest("mission.status"), "invalid-argument"),
        (CommandRequest("mission.list", request_id="../bad"), "invalid-request"),
        (CommandRequest("mission.list", target="workstation"), "invalid-target"),
    ],
)
def test_invalid_requests_are_rejected_before_any_effect(mission, request_, code):
    state, _ = mission
    before = _commands(state)
    result = commands.execute(request_)
    assert (result.outcome, result.error.code if result.error else None) == ("rejected", code)
    assert _commands(state) == before


def test_request_json_round_trip_and_strict_decoding():
    request = CommandRequest("mission.events", {"after": 3}, mission_id="m1", request_id="r-1")
    assert CommandRequest.from_json(json.loads(json.dumps(request.to_json()))) == request
    for bad, code in [
        ([], "invalid-request"),
        ({**request.to_json(), "schema_version": 2}, "unsupported-version"),
        ({**request.to_json(), "extra": 1}, "invalid-request"),
        ({**request.to_json(), "params": []}, "invalid-request"),
        ({**request.to_json(), "mission_id": 5}, "invalid-request"),
        ({**request.to_json(), "request_id": None}, "invalid-request"),
    ]:
        with pytest.raises(CommandFailure) as caught:
            CommandRequest.from_json(bad)
        assert caught.value.code == code


def test_read_operations_match_cli_json_output(mission, capsys):
    state, mission_id = mission
    assert main(["mission", "list", "--json"]) == 0
    assert commands.execute(CommandRequest("mission.list")).to_json()["result"] == json.loads(capsys.readouterr().out)
    assert main(["events", "--mission", mission_id, "--json", "--limit", "5"]) == 0
    cli_events = json.loads(capsys.readouterr().out)
    envelope = commands.execute(CommandRequest("mission.events", {"limit": 5}, mission_id=mission_id))
    assert (envelope.outcome, envelope.result) == ("ok", cli_events)
    status = commands.execute(CommandRequest("mission.status", mission_id=mission_id))
    assert status.outcome == "ok" and status.result["mission"]["id"] == mission_id
    missing = commands.execute(CommandRequest("mission.status", mission_id="f" * 32))
    assert (missing.outcome, missing.error.code) == ("rejected", "not-found")
    bad_limit = commands.execute(CommandRequest("mission.events", {"limit": 0}, mission_id=mission_id))
    assert bad_limit.error.code == "invalid-argument"


def test_duplicate_delivery_returns_stored_outcome_without_repeating_effect(mission):
    state, mission_id = mission
    first = commands.execute(CommandRequest("mission.cancel", mission_id=mission_id, request_id="req-cancel-1"))
    assert first.outcome == "applied" and first.result["status"] == "APPLIED"
    with BridgeStore(state / "bridge.sqlite3") as store:
        events_after_first = len(store.events(mission_id, limit=1000))
    duplicate = commands.execute(CommandRequest("mission.cancel", mission_id=mission_id, request_id="req-cancel-1"))
    assert duplicate.to_json() == first.to_json()
    assert _commands(state) == [{"id": "req-cancel-1", "status": "APPLIED"}]
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert len(store.events(mission_id, limit=1000)) == events_after_first
    reused = commands.execute(CommandRequest("mission.pause", mission_id=mission_id, request_id="req-cancel-1"))
    assert (reused.outcome, reused.error.code) == ("rejected", "conflict")


def _reject_continue(monkeypatch):
    def refuse(self, mission_id):
        raise TransitionError("no review hold to release")

    monkeypatch.setattr(BridgeStore, "release_review_hold", refuse)


def test_rejected_mutation_reports_rejected_outcome(mission, monkeypatch):
    _, mission_id = mission
    _reject_continue(monkeypatch)
    result = commands.execute(CommandRequest("mission.continue", mission_id=mission_id, request_id="req-continue"))
    assert result.outcome == "rejected" and result.error.code == "rejected"
    assert result.result["status"] == "REJECTED"


def test_apply_rechecks_status_under_the_write_lock(mission):
    """A second delivery that enqueued before the first finished must not re-apply."""
    state, mission_id = mission
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.enqueue_command(mission_id, {"action": "cancel"}, command_id="race-1")
        stored = store.finish_command("race-1", status="APPLIED", result={"from": "first delivery"})
        real_enqueue = store.enqueue_command
        store.enqueue_command = lambda *a, **k: {**real_enqueue(*a, **k), "status": "QUEUED"}  # stale view of the row
        result = bridge._apply_operator_command(store, mission_id, {"action": "cancel"}, command_id="race-1")
        assert result == stored
        assert store.get_mission(mission_id)["status"] == "CREATED"


def test_unexpected_failure_of_a_mutation_is_uncertain(mission, monkeypatch):
    _, mission_id = mission

    def boom(*args, **kwargs):
        raise RuntimeError("disk vanished")

    monkeypatch.setattr(bridge, "_apply_operator_command", boom)
    result = commands.execute(CommandRequest("mission.pause", mission_id=mission_id))
    assert result.outcome == "uncertain"
    assert result.error.to_json() == {"code": "internal", "message": "RuntimeError while executing the command", "details": {}}
    monkeypatch.setattr(bridge, "status_snapshot", boom)
    assert commands.execute(CommandRequest("mission.status", mission_id=mission_id)).outcome == "failed"


def test_error_mapping_is_stable():
    assert commands.error_for(KeyError("unknown task: 4")).code == "not-found"
    assert commands.error_for(KeyError()).message == "not found"
    assert commands.error_for(TransitionError("x")).code == "conflict"
    assert commands.error_for(ValueError("x")).code == "invalid-argument"


def test_target_option(mission, capsys):
    state, mission_id = mission
    assert main(["--target", "local", "status", "--mission", mission_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mission"]["id"] == mission_id
    assert main(["--target=local", "mission", "list", "--json"]) == 0
    capsys.readouterr()
    for argv, message in [
        (["--target", "workstation", "status", "--mission", mission_id], "no coordinator is configured"),
        (["--target", "all", "mission", "list"], "no coordinator is configured"),
        (["--target", "Bad!", "status"], "invalid target name"),
        (["--target"], "--target needs a name"),
        (["--target", "--json"], "--target needs a name"),
        (["--target", "local"], "--target needs a command"),
    ]:
        assert main(argv) == 2
        assert message in capsys.readouterr().err
    # A named target never falls back to local execution.
    assert _commands(state) == []


def test_cli_mission_controls_route_through_the_command_layer(mission, capsys, monkeypatch):
    _, mission_id = mission
    _reject_continue(monkeypatch)
    seen = []
    real = commands.dispatch
    monkeypatch.setattr(commands, "dispatch", lambda request: seen.append(request.operation) or real(request))
    assert main(["pause", "--mission", mission_id, "--json"]) == 0
    assert main(["continue", "--mission", mission_id]) == 2
    assert main(["cancel", "--mission", mission_id, "--json"]) == 0
    assert main(["stop", "--mission", mission_id, "--json"]) == 0
    capsys.readouterr()
    assert seen == ["mission.pause", "mission.continue", "mission.cancel", "mission.stop"]


def _first_task(state, mission_id):
    with BridgeStore(state / "bridge.sqlite3") as store:
        return store.list_tasks(mission_id)[0]


def test_task_reads_match_cli_and_report_missing_tasks(mission, capsys):
    state, mission_id = mission
    task = _first_task(state, mission_id)
    assert main(["task", "show", "--mission", mission_id, "--task", "1", "--json"]) == 0
    shown = commands.execute(CommandRequest("task.show", {"task_id": task["id"]}, mission_id=mission_id))
    assert shown.result == json.loads(capsys.readouterr().out)
    assert main(["task", "list", "--mission", mission_id, "--json"]) == 0
    listed = commands.execute(CommandRequest("task.list", mission_id=mission_id))
    assert listed.result["tasks"] == json.loads(capsys.readouterr().out)
    assert (listed.result["total"], listed.result["offset"]) == (1, 0)
    assert commands.execute(CommandRequest("task.show", {"task_id": 999}, mission_id=mission_id)).error.code == "not-found"
    assert commands.execute(CommandRequest("task.list", {"limit": 5000}, mission_id=mission_id)).error.code == "invalid-argument"


def test_task_add_through_cli_and_layer_store_identical_payloads(mission, capsys):
    state, mission_id = mission
    assert main(["task", "add", "--mission", mission_id, "--prompt", "Second", "--acceptance", "tests pass", "--json"]) == 0
    capsys.readouterr()
    added = commands.execute(CommandRequest("task.add", {"prompt": "Second", "acceptance": ["tests pass"]}, mission_id=mission_id, request_id="add-2"))
    assert added.outcome == "applied" and added.result["result"]["original_prompt"] == "Second"
    with BridgeStore(state / "bridge.sqlite3") as store:
        payloads = [row[0] for row in store.db.execute("SELECT payload_json FROM commands WHERE payload_json LIKE '%task.add%' ORDER BY rowid")]
    assert len(payloads) == 2 and payloads[0] == payloads[1]
    bad = commands.execute(CommandRequest("task.add", {"prompt": "x", "depends_on": ["1"]}, mission_id=mission_id))
    assert bad.error.code == "invalid-argument" and "list of int" in bad.error.message


def test_stale_revision_is_a_conflict_and_changes_nothing(mission):
    state, mission_id = mission
    task = _first_task(state, mission_id)
    stale = commands.execute(CommandRequest("task.priority", {"task_id": task["id"], "priority": 7, "expected_revision": task["revision"] + 1}, mission_id=mission_id))
    assert (stale.outcome, stale.error.code) == ("rejected", "conflict")
    assert _first_task(state, mission_id)["priority"] == task["priority"]
    fresh = commands.execute(CommandRequest("task.priority", {"task_id": task["id"], "priority": 7, "expected_revision": task["revision"]}, mission_id=mission_id))
    assert fresh.outcome == "applied" and _first_task(state, mission_id)["priority"] == 7


def test_conflicting_amendments_only_one_wins(mission):
    """Two clients amend the same revision; the second is rejected, not silently applied (TEST-007)."""
    state, mission_id = mission
    task = _first_task(state, mission_id)
    first = commands.execute(CommandRequest("task.amend", {"task_id": task["id"], "prompt": "A", "expected_revision": task["revision"]}, mission_id=mission_id))
    second = commands.execute(CommandRequest("task.amend", {"task_id": task["id"], "prompt": "B", "expected_revision": task["revision"]}, mission_id=mission_id))
    assert first.outcome == "applied" and (second.outcome, second.error.code) == ("rejected", "conflict")


def test_cli_task_mutations_route_through_the_command_layer(mission, capsys, monkeypatch):
    _, mission_id = mission
    seen = []
    real = commands.dispatch
    monkeypatch.setattr(commands, "dispatch", lambda request: seen.append(request.operation) or real(request))
    for argv in (["priority", "--task", "1", "--value", "3"], ["reorder", "--task", "1", "--value", "2"], ["pause", "--task", "1"], ["resume", "--task", "1"],
                 ["amend", "--task", "1", "--prompt", "new"], ["cancel", "--task", "1"]):
        assert main(["task", argv[0], "--mission", mission_id, *argv[1:], "--json"]) == 0, argv
    capsys.readouterr()
    assert seen == ["task.priority", "task.reorder", "task.pause", "task.resume", "task.amend", "task.cancel"]


def test_remote_style_references_resolve_on_the_worker(mission):
    """Requests may name a mission by unique prefix and a task by its number, never by folder."""
    state, mission_id = mission
    task = _first_task(state, mission_id)
    shown = commands.execute(CommandRequest("task.show", {"task_number": 1}, mission_id=mission_id[:8]))
    assert shown.outcome == "ok" and shown.result["id"] == task["id"]
    for params, code in [({}, "invalid-argument"), ({"task_number": 1, "task_id": task["id"]}, "invalid-argument"), ({"task_number": 9}, "not-found")]:
        assert commands.execute(CommandRequest("task.show", params, mission_id=mission_id)).error.code == code
    paused = commands.execute(CommandRequest("task.pause", {"task_number": 1}, mission_id=mission_id[:8], request_id="pause-by-number"))
    assert paused.outcome == "applied"
    with BridgeStore(state / "bridge.sqlite3") as store:
        payload = json.loads(store.db.execute("SELECT payload_json FROM commands WHERE id='pause-by-number'").fetchone()[0])
    assert payload == {"action": "task.pause", "task_id": task["id"]}
    assert commands.execute(CommandRequest("mission.status", mission_id="zz")).error.code == "invalid-argument"


def test_proposal_operations_list_and_decide_by_task_number(mission, capsys, monkeypatch):
    state, mission_id = mission
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.set_mission_status(mission_id, "RUNNING")
        source = _first_task(state, mission_id)
        operation = store.begin_operation(source["id"], iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
    for prompt in ("Keep", "Drop"):
        assert main(["proposal", "create", "--mission", mission_id, "--operation", operation, "--prompt", prompt, "--json"]) == 0
    capsys.readouterr()
    pending = commands.execute(CommandRequest("proposal.list", mission_id=mission_id))
    assert [item["original_prompt"] for item in pending.result["proposals"]] == ["Keep", "Drop"]
    assert [item["original_prompt"] for item in commands.execute(CommandRequest("proposal.list", {"after": 2}, mission_id=mission_id)).result["proposals"]] == ["Drop"]
    assert commands.execute(CommandRequest("proposal.list", {"status": "maybe"}, mission_id=mission_id)).error.code == "invalid-argument"
    assert commands.execute(CommandRequest("proposal.list", {"limit": 0}, mission_id=mission_id)).error.code == "invalid-argument"
    accepted = commands.execute(CommandRequest("proposal.decide", {"task_number": 2, "accept": True}, mission_id=mission_id[:8], request_id="accept-2"))
    assert accepted.outcome == "applied" and accepted.result["result"]["proposal_status"] == "ACCEPTED"
    seen = []
    real = commands.dispatch
    monkeypatch.setattr(commands, "dispatch", lambda request: seen.append(request.operation) or real(request))
    assert main(["proposal", "reject", "--mission", mission_id, "--task", "3", "--reason", "no", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["proposal_status"] == "REJECTED"
    assert main(["proposal", "list", "--mission", mission_id, "--status", "ALL", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["proposals"]) == 2
    assert seen == ["proposal.decide", "proposal.list"]
    again = commands.execute(CommandRequest("proposal.decide", {"task_number": 2, "accept": False}, mission_id=mission_id))
    assert again.outcome == "rejected"


def test_console_commands_reach_the_same_command_layer(mission, monkeypatch):
    """CLI-002: console mutations go through the CLI into the same typed operations as remote and web requests."""
    _, mission_id = mission
    seen = []
    real = commands.dispatch
    monkeypatch.setattr(commands, "dispatch", lambda request: seen.append(request.operation) or real(request))
    for line in ("priority 1 5", "pause 1", "resume 1", "cancel 1"):
        bridge.dispatch_console_command(mission_id, line)
    assert seen == ["task.priority", "task.pause", "task.resume", "task.cancel"]
