"""Configured acceptance verification uses bounded, private evidence."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time

from llm_tools.bridge_runtime import run_configured_verifier
from llm_tools.bridge_store import BridgeStore


def _verifying_mission(tmp_path, verifier):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    artifact = cwd / "result.txt"
    artifact.write_text("verified artifact\n", encoding="utf-8")
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=cwd, config={"verifier": verifier})
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "work", acceptance=["result.txt exists"])
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    return store, mission, task_id


def test_verifier_success_records_revision_bound_artifact_digest(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": ["python", "-c", "import json; print(json.dumps({'passed': True, 'summary': 'checks passed', 'artifacts': ['result.txt']}))"], "timeout_seconds": 5},
    )

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert outcome["status"] == "COMPLETED"
    task = store.get_task(task_id)
    assert task["evidence"][-1]["source"] == "verifier"
    assert task["evidence"][-1]["revision"] == task["revision"]
    assert len(task["evidence"][-1]["digest"]) == 64
    assert task["evidence"][-1]["artifacts"][0]["path"] == "result.txt"
    assert Path(outcome["logs_path"]).stat().st_mode & 0o777 == 0o600
    managed = store.db.execute("SELECT status,process_pid,process_start FROM managed_processes WHERE mission_id=?", (mission["id"],)).fetchone()
    assert managed["status"] == "EXITED"
    assert managed["process_pid"] > 0 and managed["process_start"]


def test_verifier_failure_leaves_task_verifying_and_records_private_result(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": ["python", "-c", "print('{\"passed\": false, \"summary\": \"check failed\"}')"], "timeout_seconds": 5},
    )

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert outcome["status"] == "VERIFYING"
    assert store.get_task(task_id)["status"] == "VERIFYING"
    assert outcome["passed"] is False
    assert store.events(mission["id"])[-1]["kind"] == "verification-failed"


def test_verifier_timeout_is_bounded_and_does_not_complete_task(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": ["python", "-c", "import time; time.sleep(1)"], "timeout_seconds": 1},
    )
    store.get_mission(mission["id"])  # keep this contract independent of global CLI config

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime", timeout_override=0.05)

    assert outcome["status"] == "VERIFYING"
    assert outcome["reason"] == "timeout"


def test_verifier_output_memory_and_private_log_are_bounded(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": ["python", "-c", "print('x' * 2_000_000)"], "timeout_seconds": 5},
    )

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    log = Path(outcome["logs_path"])
    assert outcome["status"] == "VERIFYING"
    assert outcome["reason"] == "output-too-large"
    assert outcome["output_size"] > 1_000_000
    assert log.stat().st_size <= 10 * 1024 * 1024 + 64


def test_verifier_logs_are_immutable_per_revision_and_attempt(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": ["python", "-c", "print('{\"passed\": false}')"]},
    )

    first = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")
    second = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert first["logs_path"] != second["logs_path"]
    assert Path(first["logs_path"]).exists()
    assert Path(second["logs_path"]).exists()
    assert f"{task_id}-r1-" in Path(first["logs_path"]).name
    assert f"{task_id}-r1-" in Path(second["logs_path"]).name


def test_task_amendment_during_verifier_cannot_commit_old_revision_evidence(tmp_path) -> None:
    started = tmp_path / "verifier-started"
    release = tmp_path / "verifier-release"
    script = tmp_path / "wait_verifier.py"
    script.write_text(
        "import json,time\nfrom pathlib import Path\n"
        f"Path({str(started)!r}).write_text('started')\n"
        f"release=Path({str(release)!r})\n"
        "while not release.exists(): time.sleep(.01)\n"
        "print(json.dumps({'passed': True, 'summary': 'old revision'}))\n",
        encoding="utf-8",
    )
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": [sys.executable, str(script)], "timeout_seconds": 5},
    )
    result: dict[str, object] = {}

    def verify_in_separate_connection() -> None:
        with BridgeStore(store.path) as verifier_store:
            result.update(run_configured_verifier(verifier_store, mission["id"], task_id, runtime=tmp_path / "runtime"))

    thread = threading.Thread(target=verify_in_separate_connection)
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not started.exists():
        time.sleep(0.01)
    assert started.exists()
    store.amend_task(task_id, prompt="new revision")
    release.write_text("continue", encoding="ascii")
    thread.join(timeout=5)

    assert not thread.is_alive()
    task = store.get_task(task_id)
    assert task["status"] == "READY"
    assert task["revision"] == 2
    assert task["prompt"] == "new revision"
    assert task["evidence"] == []
    assert result["passed"] is False
    assert result["reason"] == "task-changed-during-verification"
    assert not any(event["kind"] == "task-verified" for event in store.events(mission["id"]))


def test_verifier_timeout_kills_descendant_process_group(tmp_path) -> None:
    marker = tmp_path / "late-child-marker"
    code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c',\"import time; time.sleep(.8); open('late-child-marker','w').write('bad')\"]); "
        "print('spawned', flush=True); import time; time.sleep(30)"
    )
    store, mission, task_id = _verifying_mission(tmp_path, {"argv": ["python", "-c", code], "timeout_seconds": 0.1})

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    import time

    time.sleep(1)
    assert outcome["reason"] == "timeout"
    assert not marker.exists()
