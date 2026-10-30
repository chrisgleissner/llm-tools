"""A provider launched by Bridge (LLM_BRIDGE_MANAGED=1) may not run operator commands."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from llm_tools import bridge
from llm_tools.bridge import MANAGED_REFUSAL, dispatch_console_command, main
from llm_tools.bridge_store import (
    MAX_PROPOSAL_BYTES,
    MAX_PROPOSALS_PER_OPERATION,
    BridgeStore,
    TransitionError,
)


@pytest.fixture()
def mission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    monkeypatch.delenv("LLM_BRIDGE_MANAGED", raising=False)
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission_id = store.create_mission("managed", "Guard", cwd=tmp_path)["id"]
        task = store.add_task(mission_id, "Original")
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(task, iteration=1, station="fixture")
        proposal = store.propose_task(mission_id, "Follow-up", source_operation_id=operation)
        ordinal = store.get_task(proposal)["ordinal"]
        original_ordinal = store.get_task(task)["ordinal"]
    return {"id": mission_id, "task": task, "operation": operation, "proposal": proposal, "proposal_ordinal": ordinal, "task_ordinal": original_ordinal}


def operator_commands(mission: dict[str, object]) -> list[list[str]]:
    mid = str(mission["id"])
    proposal = str(mission["proposal_ordinal"])
    task = str(mission["task_ordinal"])
    return [
        ["proposal", "accept", "--mission", mid, "--task", proposal],
        ["proposal", "reject", "--mission", mid, "--task", proposal],
        ["task", "verify", "--mission", mid, "--task", task, "--approve"],
        ["task", "cancel", "--mission", mid, "--task", task],
        ["task", "add", "--mission", mid, "--prompt", "injected"],
        ["cancel", "--mission", mid],
        ["stop", "--mission", mid],
        ["pause", "--mission", mid],
        ["steer", "--mission", mid, "--kind", "stop-after-task", "--task", task],
        ["start", "--mission", mid],
        ["mission", "create", "--prompt", "injected", "--cwd", "."],
        ["--prompt", "injected"],
    ]


def test_managed_provider_cannot_run_operator_commands(mission: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_BRIDGE_MANAGED", "1")
    for command in operator_commands(mission):
        assert main(command) == 2, command
        assert MANAGED_REFUSAL in capsys.readouterr().err, command
    with BridgeStore(Path(os.environ["LLM_BRIDGE_STATE_DIR"]) / "bridge.sqlite3") as store:
        assert store.get_task(int(mission["proposal"]))["proposal_status"] == "PENDING"
        assert store.get_mission(str(mission["id"]))["status"] == "RUNNING"
        assert store.get_task(int(mission["task"]))["status"] != "CANCELLED"


def test_managed_provider_keeps_read_only_commands_and_proposal_create(mission: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_BRIDGE_MANAGED", "1")
    mid = str(mission["id"])
    allowed = [
        ["status", "--mission", mid, "--json"],
        ["events", "--mission", mid, "--json"],
        ["task", "list", "--mission", mid, "--json"],
        ["task", "show", "--mission", mid, "--task", str(mission["task_ordinal"]), "--json"],
        ["proposal", "list", "--mission", mid, "--json"],
        ["mission", "list", "--json"],
        ["proposal", "create", "--mission", mid, "--operation", str(mission["operation"]), "--prompt", "More work", "--json"],
    ]
    for command in allowed:
        assert main(command) == 0, (command, capsys.readouterr().err)
        capsys.readouterr()


def test_console_never_inherits_the_managed_marker(mission: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator console may be started from any shell; its commands must still work."""
    monkeypatch.setenv("LLM_BRIDGE_MANAGED", "1")
    response = dispatch_console_command(str(mission["id"]), f"proposal accept --task {mission['proposal_ordinal']}")
    assert json.loads(response)["status"] == "APPLIED", response


def test_controller_launch_drops_the_managed_marker(mission: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    from llm_tools import bridge_runtime

    monkeypatch.setenv("LLM_BRIDGE_MANAGED", "1")
    captured: dict[str, object] = {}

    class Stop(Exception):
        pass

    def fake_popen(command, **kwargs):
        captured["env"] = kwargs["env"]
        raise Stop

    with monkeypatch.context() as patch:
        patch.setattr(bridge_runtime.subprocess, "Popen", fake_popen)
        with pytest.raises(Stop):
            bridge_runtime.launch_controller(str(mission["id"]), headless=True)
    assert "LLM_BRIDGE_MANAGED" not in captured["env"]  # type: ignore[operator]


def test_proposal_size_is_capped(mission: dict[str, object]) -> None:
    state = Path(os.environ["LLM_BRIDGE_STATE_DIR"])
    with BridgeStore(state / "bridge.sqlite3") as store:
        store.propose_task(str(mission["id"]), "x" * MAX_PROPOSAL_BYTES, source_operation_id=str(mission["operation"]))
        with pytest.raises(ValueError, match="larger than 64 KiB"):
            store.propose_task(str(mission["id"]), "x" * (MAX_PROPOSAL_BYTES + 1), source_operation_id=str(mission["operation"]))
        with pytest.raises(ValueError, match="larger than 64 KiB"):
            store.propose_task(str(mission["id"]), "é" * MAX_PROPOSAL_BYTES, source_operation_id=str(mission["operation"]))


def test_proposal_count_per_operation_is_capped(mission: dict[str, object]) -> None:
    state = Path(os.environ["LLM_BRIDGE_STATE_DIR"])
    with BridgeStore(state / "bridge.sqlite3") as store:
        for index in range(MAX_PROPOSALS_PER_OPERATION - 1):  # the fixture already made one
            store.propose_task(str(mission["id"]), f"idea {index}", source_operation_id=str(mission["operation"]))
        with pytest.raises(TransitionError, match="already made"):
            store.propose_task(str(mission["id"]), "one too many", source_operation_id=str(mission["operation"]))
        reply = bridge._apply_operator_command(store, str(mission["id"]), {
            "action": "task.propose", "source_operation_id": str(mission["operation"]), "prompt": "again",
        })
    assert reply["status"] == "REJECTED"


def test_controls_advertised_to_providers_are_only_the_allowed_ones() -> None:
    source = Path(bridge.__file__).with_name("bridge_runtime.py").read_text(encoding="utf-8")
    assert '"pause": f"llm-bridge pause' not in source and '"stop": f"llm-bridge stop' not in source


def test_managed_provider_is_scoped_to_its_own_mission(mission: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path_factory: pytest.TempPathFactory) -> None:
    """With LLM_BRIDGE_MISSION_ID=A, naming mission B (or inferring it from the folder) is refused."""
    own = str(mission["id"])
    other_dir = tmp_path_factory.mktemp("other")
    with BridgeStore(Path(os.environ["LLM_BRIDGE_STATE_DIR"]) / "bridge.sqlite3") as store:
        other = str(store.create_mission("other", "Secret objective", cwd=other_dir)["id"])
        store.add_task(other, "Secret prompt of the other mission")
        store.set_mission_status(other, "RUNNING")
        other_operation = store.begin_operation(store.add_task(other, "Second"), iteration=1, station="fixture")
    monkeypatch.setenv("LLM_BRIDGE_MANAGED", "1")
    monkeypatch.setenv("LLM_BRIDGE_MISSION_ID", own)
    for command in (
        ["task", "show", "--mission", other, "--task", "1"],
        ["task", "list", "--mission", other],
        ["status", "--mission", other],
        ["events", "--mission", other],
        ["proposal", "list", "--mission", other[:8]],
        ["proposal", "create", "--mission", other, "--operation", str(other_operation), "--prompt", "Injected"],
    ):
        assert main(command) == 2, command
        captured = capsys.readouterr()
        assert "own mission" in captured.err, (command, captured.err)
        assert "Secret prompt" not in captured.out + captured.err
    # Omitting --mission uses the caller's own mission even when the folder belongs to another one.
    monkeypatch.chdir(other_dir)
    assert main(["task", "show", "--task", str(mission["task_ordinal"]), "--json"]) == 0
    assert "Secret prompt" not in capsys.readouterr().out
    # The caller's own mission still works, by full id and by prefix.
    for ref in (own, own[:8]):
        assert main(["task", "list", "--mission", ref, "--json"]) == 0
        capsys.readouterr()
