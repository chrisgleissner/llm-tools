"""A verifier the agent edited cannot complete its own task without operator approval."""

from __future__ import annotations

import json
import sys

from llm_tools.bridge import main
from llm_tools.bridge_runtime import run_configured_verifier
from llm_tools.bridge_store import BridgeStore
from llm_tools.bridge_verifier_guard import changed_verifier_files, verifier_fingerprints


def _scripted_verifier(tmp_path, *, extra_files=None):
    """A verifier script inside the repo, configured the way ``mission create`` configures it."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    script = cwd / "check.py"
    script.write_text("print('{\"passed\": true, \"summary\": \"original check\"}')\n", encoding="utf-8")
    (cwd / "data.txt").write_text("data\n", encoding="utf-8")
    argv = [sys.executable, "check.py"]
    fingerprints = verifier_fingerprints(argv, cwd, extra_files)
    return script, {"argv": argv, "timeout_seconds": 5, "fingerprints": fingerprints}, fingerprints


def _mission_for(tmp_path, verifier):
    cwd = tmp_path / "repo"
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=cwd, config={"verifier": verifier})
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "work")
    store.finish_operation(store.begin_operation(task_id, iteration=1, station="codex"), exit_code=0)
    return store, mission, task_id


def test_unmodified_script_verifier_still_completes_the_task(tmp_path) -> None:
    _script, verifier, fingerprints = _scripted_verifier(tmp_path)
    assert list(fingerprints) == ["check.py"]  # the interpreter lives outside the repo and is not tracked
    store, mission, task_id = _mission_for(tmp_path, verifier)
    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")
    assert outcome["status"] == "COMPLETED" and outcome["passed"] is True


def test_verifier_edited_by_the_agent_requires_operator_approval(tmp_path) -> None:
    script, verifier, _ = _scripted_verifier(tmp_path)
    store, mission, task_id = _mission_for(tmp_path, verifier)
    script.write_text("print('{\"passed\": true, \"summary\": \"rewritten by the agent\"}')\n", encoding="utf-8")

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert outcome["status"] == "VERIFYING" and outcome["passed"] is False
    assert outcome["reason"] == "verifier-modified"
    assert outcome["changed_files"] == ["check.py"]
    assert store.get_task(task_id)["status"] == "VERIFYING"
    assert not store.get_task(task_id)["evidence"]
    event = store.events(mission["id"])[-1]
    assert event["kind"] == "verification-failed"
    assert "check.py" in json.dumps(event) and "operator approval required" in json.dumps(event)
    # The operator can still approve it explicitly.
    store.verify_task(task_id, evidence="reviewed the verifier change", source="operator")
    assert store.get_task(task_id)["status"] == "COMPLETED"


def test_deleted_or_extra_tracked_verifier_file_requires_approval(tmp_path) -> None:
    script, verifier, fingerprints = _scripted_verifier(tmp_path, extra_files=["data.txt"])
    assert set(fingerprints) == {"check.py", "data.txt"}
    store, mission, task_id = _mission_for(tmp_path, verifier)
    (tmp_path / "repo" / "data.txt").write_text("tampered\n", encoding="utf-8")
    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")
    assert outcome["reason"] == "verifier-modified" and outcome["changed_files"] == ["data.txt"]
    script.unlink()
    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")
    assert outcome["changed_files"] == ["check.py", "data.txt"]


def test_repointed_symlink_and_outside_paths_are_handled(tmp_path) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (tmp_path / "outside.sh").write_text("exit 0\n", encoding="utf-8")
    (cwd / "a.sh").write_text("exit 0\n", encoding="utf-8")
    (cwd / "b.sh").write_text("exit 0\n", encoding="utf-8")
    (cwd / "link.sh").symlink_to(cwd / "a.sh")
    assert verifier_fingerprints([str(tmp_path / "outside.sh")], cwd) == {}
    assert verifier_fingerprints([], cwd) == {}
    assert verifier_fingerprints(["./../outside.sh"], cwd) == {}
    config = {"fingerprints": verifier_fingerprints(["./link.sh"], cwd)}
    assert list(config["fingerprints"]) == ["link.sh"]
    assert changed_verifier_files(config, cwd) == []
    (cwd / "link.sh").unlink()
    (cwd / "link.sh").symlink_to(cwd / "b.sh")  # identical content, different target
    assert changed_verifier_files(config, cwd) == ["link.sh"]
    assert changed_verifier_files({}, cwd) == []
    assert changed_verifier_files({"fingerprints": {"../outside.sh": "x"}}, cwd) == ["../outside.sh"]


def test_mission_create_records_verifier_fingerprints(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "verify.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repo / "verify.sh").chmod(0o755)
    (repo / "extra.cfg").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert main(["mission", "create", "--prompt", "work", "--cwd", str(repo), "--headless", "--providers", "codex",
                 "--verifier-arg", "./verify.sh", "--verifier-file", "extra.cfg", "--json"]) == 0
    with BridgeStore(tmp_path / "state" / "bridge.sqlite3") as store:
        mission = store.list_missions()[0]
    assert set(mission["config"]["verifier"]["fingerprints"]) == {"verify.sh", "extra.cfg"}
