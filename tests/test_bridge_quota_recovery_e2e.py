"""Quota errors must re-enter capacity selection without losing task amendments."""

from __future__ import annotations

import json

import pytest
import shlex
import warnings
import sys
import threading
import time
from pathlib import Path

from llm_tools import common, ralph_robin, scheduler
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_store import BridgeStore
from tests.conftest import wait_for


def _codex_command(tmp_path: Path, body: str) -> tuple[Path, str]:
    # A compile-time warning in the fake provider goes to its stderr. Text such as
    # 'Did you mean "!="?' then looks like a question to the scheduler's prompt watchdog,
    # which aborts the run as an interactive prompt when the output stalls (it did under
    # coverage). Refuse such a fake here instead of letting the test hang.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(body, "fake-codex", "exec")
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    binary.chmod(0o755)
    return binary, shlex.join([str(binary), "exec", "--json", "{prompt_file}"])


def _ready_payload() -> dict[str, object]:
    return {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}


def test_structured_native_75_applies_amendment_waits_for_capacity_and_runs_next_operation(
    tmp_path: Path, env: dict[str, str], monkeypatch,
) -> None:
    database = tmp_path / "bridge.sqlite3"
    usage_file = tmp_path / "usage.json"
    usage_file.write_text(json.dumps({"codex": _ready_payload()}), encoding="utf-8")
    monkeypatch.setenv("LLM_SCHEDULER_USAGE_JSON_FILE", str(usage_file))
    monkeypatch.delenv("LLM_USAGE_NOW_EPOCH", raising=False)
    monkeypatch.setattr(ralph_robin, "wait_until_epoch", lambda *_args, **_kwargs: True)

    active = tmp_path / "provider-active"
    release = tmp_path / "release-provider"
    attempts = tmp_path / "attempts.jsonl"
    reset_file = tmp_path / "quota-reset-epoch"
    script = f"""from pathlib import Path
import json, os, sys, time
attempts = Path({str(attempts)!r})
number = len(attempts.read_text().splitlines()) + 1 if attempts.exists() else 1
with attempts.open('a', encoding='utf-8') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if number == 1:
    Path({str(active)!r}).touch()
    deadline = time.monotonic() + 10
    while not Path({str(release)!r}).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    reset = int(time.time()) + 5
    Path({str(reset_file)!r}).write_text(str(reset), encoding='utf-8')
    Path(os.environ['LLM_SCHEDULER_USAGE_JSON_FILE']).write_text(json.dumps({{
        'codex': {{'available': True,
                   'five_hour': {{'remaining': 0, 'resets_at': reset}},
                   'week': {{'remaining': 0, 'resets_at': reset}}}}
    }}), encoding='utf-8')
    print(json.dumps({{'type': 'error', 'message': 'You’ve reached your usage limit. Please wait for limit to reset.'}}), flush=True)
    raise SystemExit(75)
print(json.dumps({{'type': 'item.completed', 'item': {{'type': 'agent_message', 'text': 'completed after capacity recovery'}}}}), flush=True)
"""
    _binary, command_template = _codex_command(tmp_path, script)

    with BridgeStore(database) as store:
        mission = store.create_mission("quota continuation", "mission objective", cwd=tmp_path)
        old_task = store.add_task(mission["id"], "older task awaiting verification")
        task_id = store.add_task(mission["id"], "current task")
        store.db.execute("UPDATE tasks SET status='VERIFYING' WHERE id=?", (old_task,))
        store.set_mission_status(mission["id"], "RUNNING")

    hooks = BridgeMissionHooks(
        mission["id"], handle=None, headless=True,
        controller_logs=tmp_path / "controller.log",
    )
    hooks.bind(BridgeStore(database))
    wake_targets: list[int] = []

    def prepare_suspend(target: int):
        wake_targets.append(target)
        return target, common.WakeArm(True, "fixture-systemd-timer", True, unit="fixture", target_epoch=target)

    hooks.suspend_allowed = lambda: True
    hooks.prepare_suspend = prepare_suspend

    def simulate_verified_wake(target: int, **kwargs) -> common.SuspendOutcome:
        arm = kwargs["wake_arm"]
        assert arm.armed and arm.confirmed and arm.target_epoch == target
        usage_file.write_text(json.dumps({"codex": _ready_payload()}), encoding="utf-8")
        return common.SuspendOutcome(
            True, target, target, 0, True, "fixture-systemd-timer", "ok",
            sleep_observed=True,
        )

    monkeypatch.setattr(common, "suspend_with_wake", simulate_verified_wake)

    amendment_errors: list[BaseException] = []
    amendment_applied = threading.Event()

    def amend_during_provider_run() -> None:
        try:
            wait_for(active.exists, timeout=5)
            with BridgeStore(database) as store:
                store.amend_task(task_id, prompt="amended while the first provider was active")
            amendment_applied.set()
        except BaseException as exc:  # surface helper-thread failures in the test thread
            amendment_errors.append(exc)
        finally:
            release.touch()

    amendment_thread = threading.Thread(target=amend_during_provider_run, daemon=True)
    amendment_thread.start()
    try:
        result = ralph_robin.main(
            [
                "--prompt", "mission objective", "--providers", "codex", "--cwd", str(tmp_path),
                "--command-template", command_template, "--retry-delays", "", "--poll-interval", "1",
                "--max-iterations", "1", "--max-duration", "15", "--min-iteration-seconds", "0",
                "--log-dir", str(tmp_path / "ralph-logs"), "--state-file", str(tmp_path / "ralph-state.json"),
            ],
            bridge_hooks=hooks,
        )
    finally:
        release.touch()
        amendment_thread.join(timeout=5)
        hooks.store.close()

    assert result == 0
    assert not amendment_thread.is_alive()
    assert not amendment_errors, amendment_errors
    assert amendment_applied.is_set()
    expected_reset = int(reset_file.read_text(encoding="utf-8"))
    assert wake_targets == [expected_reset]

    with BridgeStore(database) as store:
        operations = store.db.execute(
            "SELECT task_id, exit_code, status FROM operations WHERE mission_id=? ORDER BY rowid",
            (mission["id"],),
        ).fetchall()
        assert [(row["task_id"], row["exit_code"], row["status"]) for row in operations] == [
            (task_id, 1, "EXITED"), (task_id, 0, "EXITED"),
        ]
        assert store.get_task(old_task)["status"] == "VERIFYING"
        current = store.get_task(task_id)
        assert current["status"] == "VERIFYING"
        assert current["revision"] == 2
        assert "amended while the first provider was active" in current["prompt"]
        assert store.db.execute(
            "SELECT status FROM amendments WHERE task_id=?", (task_id,),
        ).fetchone()["status"] == "APPLIED"
        events = [event["kind"] for event in store.events(mission["id"])]
        assert "suspend-intent" in events
        assert "suspend-work-resumed" in events

    runs = [json.loads(line) for line in attempts.read_text(encoding="utf-8").splitlines()]
    assert len(runs) == 2
    assert all("--json" in argv for argv in runs)


def test_structured_interactive_quota_prompt_stays_blocked_with_pending_amendment(
    tmp_path: Path, env: dict[str, str], monkeypatch,
) -> None:
    monkeypatch.setenv(
        "LLM_SCHEDULER_USAGE_JSON",
        json.dumps({"codex": _ready_payload()}),
    )
    script = """import json
print(json.dumps({'type': 'error', 'message': 'ERROR: You’ve reached your usage limit. What do you want to do?'}), flush=True)
"""
    _binary, command_template = _codex_command(tmp_path, script)
    database = tmp_path / "bridge.sqlite3"
    with BridgeStore(database) as store:
        mission = store.create_mission("interactive quota", "objective", cwd=tmp_path)
        task_id = store.add_task(mission["id"], "task needing a decision")
        store.set_mission_status(mission["id"], "RUNNING")

    with BridgeStore(database) as store:
        hooks = BridgeMissionHooks(
            mission["id"], handle=None, headless=True,
            controller_logs=tmp_path / "controller.log",
        )
        hooks.bind(store)
        context = hooks.before_iteration()
        cfg = scheduler.SchedulerConfig(
            provider="codex", prompt_text=context["prompt"], cwd=str(tmp_path),
            log_dir=tmp_path / "interactive-logs", command_template=command_template,
            headless=True, exact_stdout=True, ralph_robin_active=True,
        )
        selection = {"provider": "codex"}
        assert hooks.before_launch(context, selection, cfg, 1)
        store.amend_task(task_id, prompt="pending amendment after interactive prompt")
        status = ralph_robin.run_scheduler_inline(cfg)
        hooks.after_iteration(context, selection, cfg, 1, status, 0.1)

        assert status == common.AUTONOMY_ABORT_STATUS
        assert store.get_task(task_id)["status"] == "BLOCKED"
        assert store.db.execute(
            "SELECT status FROM amendments WHERE task_id=?", (task_id,),
        ).fetchone()["status"] == "PENDING"
        assert store.db.execute(
            "SELECT COUNT(*) FROM operations WHERE task_id=?", (task_id,),
        ).fetchone()[0] == 1


def test_fake_provider_with_a_compile_warning_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SyntaxError):
        _codex_command(tmp_path, "if 'text' is not None:\n    pass\n")
