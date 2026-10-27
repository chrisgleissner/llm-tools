"""Configured acceptance verification uses bounded, private evidence."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time

import pytest

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


def test_verifier_private_log_caps_disk_output_above_ten_mib(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": [sys.executable, "-c", "import sys; sys.stdout.write('z' * (11 * 1024 * 1024))"], "timeout_seconds": 10},
    )

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    log = Path(outcome["logs_path"])
    assert outcome["reason"] == "output-too-large"
    assert outcome["output_size"] == 11 * 1024 * 1024
    assert log.stat().st_size <= 10 * 1024 * 1024 + 64
    assert log.read_bytes().endswith(b"[verifier output truncated at 10 MiB]\n")


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


def test_verifier_preconditions_do_not_spawn_or_change_nonverifying_task(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(tmp_path, {"argv": ["true"]})
    store.set_task_status(task_id, "CANCELLED")

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert outcome == {"status": "CANCELLED", "reason": "task-no-longer-verifying", "revision": 1}
    assert store.db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0] == 0


def test_bounded_verifier_optional_lifecycle_callbacks_can_be_omitted(tmp_path) -> None:
    from llm_tools.bridge_runtime import _run_bounded_verifier

    outcome = _run_bounded_verifier(
        [sys.executable, "-c", "print('no callbacks required')"],
        str(tmp_path),
        2,
        tmp_path / "optional.log",
    )

    assert outcome["exit_code"] == 0
    assert outcome["output"] == b"no callbacks required\n"


@pytest.mark.parametrize(
    ("verifier", "timeout_override", "message"),
    [
        ({"argv": ["bad\x00argv"]}, None, "NUL bytes"),
        ({"argv": ["true"]}, 0, "timeout must be between"),
    ],
)
def test_verifier_invalid_launch_configuration_fails_before_managed_spawn(tmp_path, verifier, timeout_override, message) -> None:
    store, mission, task_id = _verifying_mission(tmp_path, verifier)

    with pytest.raises(ValueError, match=message):
        run_configured_verifier(
            store, mission["id"], task_id, runtime=tmp_path / "runtime", timeout_override=timeout_override,
        )

    assert store.db.execute("SELECT COUNT(*) FROM managed_processes").fetchone()[0] == 0
    assert store.get_task(task_id)["status"] == "VERIFYING"


def test_verifier_missing_executable_records_private_failed_attempt(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": [str(tmp_path / "does-not-exist")], "timeout_seconds": 2},
    )

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert outcome["status"] == "VERIFYING"
    assert outcome["reason"].startswith("launch-failed:")
    assert Path(outcome["logs_path"]).stat().st_mode & 0o777 == 0o600
    attempt = store.events(mission["id"])[-1]
    assert attempt["kind"] == "verification-failed"
    managed = store.db.execute("SELECT status,exit_code FROM managed_processes").fetchone()
    assert tuple(managed) == ("FAILED", None)


def test_verifier_context_is_removed_even_if_child_deletes_it(tmp_path) -> None:
    code = "from pathlib import Path; Path(__import__('sys').argv[1]).unlink(); print('context consumed')"
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": [sys.executable, "-c", code, "{context}"], "timeout_seconds": 3},
    )

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert outcome["passed"] is True
    context_files = list((tmp_path / "runtime" / "verifier").glob("*.json"))
    assert context_files == []


def test_verifier_successful_plain_text_becomes_revision_bound_evidence(tmp_path) -> None:
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": [sys.executable, "-c", "print('all checks passed')"], "timeout_seconds": 3},
    )

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert outcome["passed"] is True
    assert outcome["summary"] == "all checks passed"
    evidence = store.get_task(task_id)["evidence"][-1]
    assert evidence["source"] == "verifier"
    assert evidence["revision"] == 1
    assert evidence["text"] == "Configured verifier passed: all checks passed"


def test_verifier_refuses_unverified_child_identity_and_marks_managed_launch_failed(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime

    store, mission, task_id = _verifying_mission(tmp_path, {"argv": [sys.executable, "-c", "import time; time.sleep(30)"]})
    monkeypatch.setattr(bridge_runtime, "_process_start_token", lambda _pid: "")

    with pytest.raises(RuntimeError, match="could not verify verifier process identity"):
        run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    managed = store.db.execute("SELECT status,process_pid,process_start FROM managed_processes").fetchone()
    assert managed["status"] == "FAILED"
    assert managed["process_pid"] is None and managed["process_start"] is None
    assert store.get_task(task_id)["status"] == "VERIFYING"


def test_verifier_timeout_escalates_when_child_ignores_sigterm(tmp_path) -> None:
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    store, mission, task_id = _verifying_mission(
        tmp_path,
        {"argv": [sys.executable, "-c", code], "timeout_seconds": 0.05},
    )

    outcome = run_configured_verifier(store, mission["id"], task_id, runtime=tmp_path / "runtime")

    assert outcome["status"] == "VERIFYING"
    assert outcome["reason"] == "timeout"
    managed = store.db.execute("SELECT status,exit_code FROM managed_processes").fetchone()
    assert managed["status"] == "EXITED" and managed["exit_code"] < 0
