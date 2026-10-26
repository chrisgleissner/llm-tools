"""Controller boundary behavior for durable Bridge missions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import shutil
import subprocess
import time

import pytest

from llm_tools import bridge
from llm_tools.bridge import _apply_operator_command
from llm_tools import common
from llm_tools import scheduler
from llm_tools.bridge_runtime import BridgeMissionHooks, terminate_managed_processes
from llm_tools.bridge_runtime import _provider_station_argv, _process_start_token, _terminate_owned_process_group, launch_controller, mission_socket_path, run_controller
from llm_tools.bridge_store import BridgeStore
from llm_tools.terminal import TerminalHandle


def test_pause_command_reports_active_provider_work_is_deferred(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    store.begin_operation(task_id, iteration=1, station="codex")

    command = _apply_operator_command(store, mission["id"], {"action": "pause"})

    assert command["status"] == "APPLIED"
    assert command["result"]["work_status"] == "DEFERRED"
    assert command["result"]["detail"] == "pause takes effect when the active provider returns"


def test_mission_tmux_socket_uses_short_private_user_runtime_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / ("runtime-" + "x" * 100)))
    path = mission_socket_path("a" * 32)

    assert len(os.fsencode(str(path.with_name(path.name + ".conf")))) < 96
    assert path.name.startswith("m-") and path.suffix == ".sock"
    assert path.parent.stat().st_uid == os.getuid()
    assert path.parent.stat().st_mode & 0o077 == 0
    assert path == mission_socket_path("a" * 32)
    assert path != mission_socket_path("b" * 32)


def test_controller_return_cannot_leave_mission_claiming_running(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    state = tmp_path / "llm-tools" / "llm-bridge"
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    store.add_task(mission["id"], "work")
    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", lambda *_args, **_kwargs: 1)

    assert run_controller(mission["id"], headless=True) == 1

    current = store.get_mission(mission["id"])
    assert current["status"] == "FAILED"
    events = store.events(mission["id"])
    assert any(event["kind"] == "controller-exited-unexpectedly" for event in events)
    mode = next(event for event in events if event["kind"] == "controller-mode")
    assert mode["data"] == {"verified": True, "actual_mode": "headless", "controller_pid": os.getpid()}


@pytest.mark.parametrize("stop_fails", [False, True])
def test_controller_tears_down_station_before_marking_mission_stopped(tmp_path, monkeypatch, stop_fails) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("llm_tools.bridge_runtime.shutil.which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    database = tmp_path / "state" / "bridge.sqlite3"
    setup = BridgeStore(database)
    mission = setup.create_mission("mission", "objective", cwd=tmp_path, config={"native_tui": True})
    teardown_statuses: list[str] = []

    class Backend:
        def start(self, **kwargs):
            socket_path = Path(kwargs["socket_path"])
            socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            socket_path.write_text("owned station marker", encoding="ascii")
            config_path = socket_path.with_name(socket_path.name + ".conf")
            config_path.write_text("owned station config", encoding="ascii")
            return TerminalHandle(socket_path, config_path, kwargs["session_name"], "%0", "%1", 6)

        def open_existing(self, socket_path, session_name, *, bridge_rows=6):
            socket_path = Path(socket_path)
            if not socket_path.exists():
                return None
            return TerminalHandle(socket_path, socket_path.with_name(socket_path.name + ".conf"), session_name, "%0", "%1", bridge_rows)

        def stop(self, _handle):
            with BridgeStore(database) as observer:
                teardown_statuses.append(observer.get_mission(mission["id"])["status"])
            if stop_fails:
                raise RuntimeError("tmux kill-server failed")
            _handle.socket_path.unlink(missing_ok=True)
            _handle.config_path.unlink(missing_ok=True)

    monkeypatch.setattr("llm_tools.bridge_runtime.TmuxTerminalBackend", Backend)

    def stop_request(_argv, *, bridge_hooks):
        with BridgeStore(database) as controller:
            controller.set_mission_status(mission["id"], "STOPPING")
        return 0

    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", stop_request)
    if stop_fails:
        with pytest.raises(RuntimeError, match="kill-server failed"):
            run_controller(mission["id"])
        assert setup.get_mission(mission["id"])["status"] == "STOPPING"
        with pytest.raises(RuntimeError, match="kill-server failed"):
            run_controller(mission["id"])
    else:
        assert run_controller(mission["id"]) == 0

    assert teardown_statuses == ["STOPPING"] if not stop_fails else ["STOPPING", "STOPPING"]
    current = setup.get_mission(mission["id"])
    assert current["status"] == ("STOPPING" if stop_fails else "STOPPED")
    runtime = tmp_path / "state" / "runtime" / f"mission-{mission['id']}"
    ready = runtime / "controller-ready.json"
    if stop_fails:
        assert not ready.exists(), "failed recovery teardown must not publish a replacement controller as ready"
    socket_path = mission_socket_path(mission["id"])
    assert socket_path.exists() is stop_fails
    assert socket_path.with_name(socket_path.name + ".conf").exists() is stop_fails


def test_start_mode_override_uses_prior_station_config_and_persists_after_cleanup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("llm_tools.bridge_runtime.shutil.which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    database = tmp_path / "state" / "bridge.sqlite3"
    setup = BridgeStore(database)
    mission = setup.create_mission("mode", "objective", cwd=tmp_path, config={"headless": False, "native_tui": True})
    old_socket = mission_socket_path(mission["id"])
    old_socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old_socket.write_text("owned old station", encoding="ascii")
    phases: list[str] = []

    class Backend:
        def open_existing(self, socket_path, session_name, *, bridge_rows=6):
            phases.append("open-existing")
            return TerminalHandle(Path(socket_path), Path(str(socket_path) + ".conf"), session_name, "%0", "%1", 6)

        def stop(self, _handle):
            phases.append("stop")
            if len([phase for phase in phases if phase == "stop"]) == 1:
                raise RuntimeError("old native station teardown failed")

        def start(self, **_kwargs):
            phases.append("start")
            return TerminalHandle(tmp_path / "new.sock", tmp_path / "new.conf", "mission", "%0", "%1", 6)

    monkeypatch.setattr("llm_tools.bridge_runtime.TmuxTerminalBackend", Backend)
    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", lambda *_args, **_kwargs: 0)

    with pytest.raises(RuntimeError, match="old native station teardown failed"):
        run_controller(mission["id"], headless=True)

    persisted = setup.get_mission(mission["id"])
    assert persisted["config"]["headless"] is False
    assert persisted["config"]["native_tui"] is True
    assert "start" not in phases


def test_controller_recovery_terminates_unresolved_verifier_without_completing_task(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    database = tmp_path / "state" / "bridge.sqlite3"
    store = BridgeStore(database)
    mission = store.create_mission("mission", "objective", cwd=tmp_path, config={"headless": True})
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    process_start = _process_start_token(process.pid)
    managed_id = store.prepare_managed_process(mission["id"], task_id, kind="verifier")
    store.mark_managed_process_running(managed_id, process_pid=process.pid, process_start=process_start)
    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", lambda *_args, **_kwargs: 0)

    try:
        assert run_controller(mission["id"], headless=True) == 0
        process.wait(timeout=4)
        assert store.get_managed_process(managed_id)["status"] == "TERMINATED"
        assert store.get_mission(mission["id"])["status"] == "FAILED"
        task = store.get_task(task_id)
        assert task["status"] == "VERIFYING"
        assert task["evidence"] == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_unidentified_starting_verifier_blocks_cleanup_instead_of_claiming_stopped(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    process_id = store.prepare_managed_process(mission["id"], task_id, kind="verifier")

    with pytest.raises(RuntimeError, match="no verified identity"):
        terminate_managed_processes(store, mission["id"])

    assert store.get_managed_process(process_id)["status"] == "STARTING"
    assert store.get_mission(mission["id"])["status"] == "RUNNING"


def test_verifier_pid_reuse_is_never_signalled_and_is_recorded_uncertain(tmp_path, monkeypatch) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    process_id = store.prepare_managed_process(mission["id"], task_id, kind="verifier")
    store.mark_managed_process_running(process_id, process_pid=1234, process_start="old:identity")
    monkeypatch.setattr("llm_tools.bridge_runtime._process_start_token", lambda _pid: "new:identity")
    monkeypatch.setattr(
        "llm_tools.bridge_runtime._terminate_owned_process_group",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not signal a reused pid")),
    )

    terminate_managed_processes(store, mission["id"])

    assert store.get_managed_process(process_id)["status"] == "UNCERTAIN"


def test_live_verifier_with_unreadable_identity_blocks_controller_cleanup(tmp_path, monkeypatch) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    process_id = store.prepare_managed_process(mission["id"], task_id, kind="verifier")
    store.mark_managed_process_running(process_id, process_pid=1234, process_start="expected")
    monkeypatch.setattr("llm_tools.bridge_runtime._process_start_token", lambda _pid: "")
    monkeypatch.setattr("llm_tools.bridge_runtime._process_alive_but_unverifiable", lambda _pid: True)

    with pytest.raises(PermissionError, match="alive but its identity cannot be verified"):
        terminate_managed_processes(store, mission["id"])

    assert store.get_managed_process(process_id)["status"] == "RUNNING"


def test_scheduler_wait_is_interruptible_within_one_poll_slice() -> None:
    calls = 0

    def interrupt() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    config = scheduler.SchedulerConfig(should_interrupt_wait=interrupt)
    started = time.monotonic()

    assert scheduler._sleep_interruptibly(config, common.now_epoch() + 30) is False
    assert time.monotonic() - started < 1.5


def test_cancel_command_is_durable_and_defers_process_boundary(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    store.begin_operation(task_id, iteration=1, station="codex")

    command = _apply_operator_command(store, mission["id"], {"action": "cancel"})

    assert command["result"]["mission_status"] == "CANCELLED"
    assert command["result"]["work_status"] == "DEFERRED"
    assert store.get_task(task_id)["status"] == "CANCELLED"


def test_task_cancel_reports_the_active_provider_operation_as_deferred(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id)

    command = _apply_operator_command(store, mission["id"], {"action": "task.cancel", "task_id": task_id})

    assert command["result"]["work_status"] == "DEFERRED"
    assert command["result"]["active_operation_id"] == operation_id
    assert "active provider returns" in command["result"]["detail"]
    assert store.get_operation(operation_id)["status"] == "RUNNING"
    assert store.get_task(task_id)["status"] == "CANCELLED"


def test_station_selection_is_persisted_as_view_only_preference(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    with bridge.open_store() as setup_store:
        mission = setup_store.create_mission(
            "mission", "objective", cwd=tmp_path,
            config={"providers": "codex,claude", "routes": ""},
        )

    response = bridge.dispatch_console_command(mission["id"], "station claude")

    assert '"status": "APPLIED"' in response
    with bridge.open_store() as store:
        current = store.get_mission(mission["id"])
        assert current["config"]["viewed_station"] == "claude"
        assert any(event["kind"] == "station-viewed" for event in store.events(mission["id"]))


def test_mission_pause_leaves_completed_work_awaiting_verification(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.set_mission_status(mission["id"], "PAUSED")

    task = store.finish_operation(operation_id, exit_code=0)

    assert task["status"] == "VERIFYING"
    assert store.get_operation(operation_id)["status"] == "EXITED"


def test_runtime_hook_keeps_autonomous_provider_launch_headless_with_control_pane(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    context = {"task_id": task_id}
    hook = BridgeMissionHooks(mission["id"], handle=SimpleNamespace(), headless=False, native_tui=False, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    config = SimpleNamespace(provider="codex", exec_mode="tmux", tmux_pane="", native_tui=False, headless=False, run_dir=None, model="")

    assert hook.before_launch(context, {"provider": "codex"}, config, 1)

    assert config.exec_mode == "fresh"
    assert config.headless is True
    assert config.native_tui is False


def test_provider_receives_explicit_bridge_identity_and_operator_controls(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "mission objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "task objective")
    store.set_mission_status(mission["id"], "RUNNING")
    context = {"task_id": task_id, "revision": store.get_task(task_id)["revision"], "prompt": "managed prompt"}
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    config = scheduler.SchedulerConfig(provider="codex", prompt_text="managed prompt", cwd=str(tmp_path), ralph_robin_active=True)

    assert hook.before_launch(context, {"provider": "codex"}, config, 1)

    env = scheduler.provider_env(config)
    assert env["LLM_BRIDGE_MANAGED"] == "1"
    assert env["LLM_BRIDGE_MISSION_ID"] == mission["id"]
    assert env["LLM_BRIDGE_TASK_ID"] == str(task_id)
    assert env["LLM_BRIDGE_TASK_REVISION"] == str(context["revision"])
    assert env["LLM_BRIDGE_OPERATION_ID"] == context["operation_id"]
    assert "llm-bridge proposal create" in env["LLM_BRIDGE_CONTROLS"]
    assert mission["id"] in env["LLM_BRIDGE_CONTROLS"] and context["operation_id"] in env["LLM_BRIDGE_CONTROLS"]
    assert "supervised by LLM Bridge" in config.prompt_text
    assert context["operation_id"] in config.prompt_text
    assert "proposal create" in config.prompt_text


def test_operation_progress_records_real_workspace_change_and_bounded_log_digest(tmp_path, monkeypatch) -> None:
    from llm_tools.bridge_progress import repository_checkpoint

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    source = repo / "main.py"
    source.write_text("before\n", encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("progress", "objective", cwd=repo)
    task_id = store.add_task(mission["id"], "implement change")
    noop_task_id = store.add_task(mission["id"], "report existing state")
    store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    output = run_dir / "attempt-1.out"
    output.write_bytes(b"provider output" * 1000)
    config = _provider_config("codex", repo, run_dir)
    config.log_dir = None
    context = {"task_id": task_id, "revision": store.get_task(task_id)["revision"]}
    before = repository_checkpoint(repo)["fingerprint"]

    assert hooks.before_launch(context, {"provider": "codex"}, config, 1)
    source.write_text("after\n", encoding="utf-8")
    after = repository_checkpoint(repo)["fingerprint"]
    hooks.after_iteration(context, {"provider": "codex"}, config, 1, 0, 0.1)

    events = store.events(mission["id"])
    checkpoint = next(event for event in events if event["kind"] == "repository-checkpoint")
    progress = next(event for event in events if event["kind"] == "operation-progress")
    assert checkpoint["data"]["operation_id"] == context["operation_id"]
    assert checkpoint["data"]["checkpoint"]["fingerprint"] == before
    assert progress["data"]["before"] == before
    assert progress["data"]["after"] == after
    assert before != after
    assert progress["data"]["output_sha256"]
    assert progress["data"]["task_id"] == task_id
    assert progress["data"]["revision"] == context["revision"]

    noop_run = tmp_path / "noop-run"
    noop_run.mkdir()
    (noop_run / "attempt-1.out").write_text("no-op provider result", encoding="utf-8")
    noop_config = _provider_config("claude", repo, noop_run)
    noop_config.log_dir = None
    noop_context = {"task_id": noop_task_id}
    assert hooks.before_launch(noop_context, {"provider": "claude"}, noop_config, 2)
    unchanged = repository_checkpoint(repo)["fingerprint"]
    hooks.after_iteration(noop_context, {"provider": "claude"}, noop_config, 2, 75, 0.1)
    noop_progress = [event for event in store.events(mission["id"]) if event["kind"] == "operation-progress"][-1]
    assert noop_progress["data"]["before"] == unchanged == noop_progress["data"]["after"]


def test_unknown_repository_progress_is_not_reported_as_unchanged(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_progress

    monkeypatch.setattr(bridge_progress, "repository_checkpoint", lambda _cwd: {"fingerprint": None, "files": 0, "bytes": 0, "reason": "unavailable"})
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("unknown progress", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = _provider_config("codex", tmp_path, run_dir)
    config.log_dir = None
    context = {"task_id": task_id}

    assert hooks.before_launch(context, {"provider": "codex"}, config, 1)
    hooks.after_iteration(context, {"provider": "codex"}, config, 1, 1, 0.1)
    events = store.events(mission["id"])
    progress = next(event for event in events if event["kind"] == "operation-progress")
    assert progress["data"]["before"] is None
    assert progress["data"]["after"] is None


def test_bounded_output_digest_hashes_only_a_stable_tail_and_rejects_symlinks(tmp_path) -> None:
    import hashlib
    from llm_tools.bridge_runtime import _bounded_output_digest

    output = tmp_path / "attempt.out"
    output.write_bytes(b"0123456789abcdefghijklmnopqrstuvwxyz")
    expected = hashlib.sha256(b"tail:36:" + b"uvwxyz").hexdigest()
    assert _bounded_output_digest(output, max_bytes=6) == expected
    link = tmp_path / "linked.out"
    link.symlink_to(output)
    assert _bounded_output_digest(link, max_bytes=6) is None


def test_task_pause_winning_prelaunch_race_aborts_prepared_operation(tmp_path, monkeypatch) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    context = {"task_id": task_id}
    config = SimpleNamespace(provider="codex", exec_mode="fresh", tmux_pane="", native_tui=False, headless=True, run_dir=None, model="")
    original = store.mark_operation_running

    def pause_before_mark(operation_id, **kwargs):
        store.set_task_status(task_id, "PAUSED")
        return original(operation_id, **kwargs)

    monkeypatch.setattr(store, "mark_operation_running", pause_before_mark)

    assert hook.before_launch(context, {"provider": "codex"}, config, 1) is False
    operation = store.db.execute("SELECT status,exit_code FROM operations").fetchone()
    assert tuple(operation) == ("EXITED", 143)
    assert store.get_task(task_id)["status"] == "PAUSED"
    event = store.events(mission["id"])[-1]
    assert event["kind"] == "operation-aborted-before-launch"
    assert event["data"]["provider_started"] is False


def test_task_prompt_contains_objective_checkpoint_and_explicit_reconstruction(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "original mission objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "original task content", title="Checkpoint task")
    with store._transaction():
        store._event(mission["id"], "checkpoint", {"summary": "prior work"}, task_id=task_id)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    context = hook.before_iteration()

    assert "original mission objective" in context["prompt"]
    assert "original task content" in context["prompt"]
    assert "prior work" in context["prompt"]
    assert "RECONSTRUCTED" in context["prompt"]
    assert "not an exact native conversation restore" in context["prompt"]


def test_runtime_hook_persists_provider_handoffs_capacity_and_exhaustion(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    hook.on_selection({
        "provider": "codex", "route": "codex-spark", "rotation_reason": "usable", "all_rate_limited": False,
        "decision": {"provider": "codex", "selected_model": "spark", "usable": True, "reason": "available"},
        "decisions": [{"provider": "codex", "route": "codex-spark", "selected_model": "spark", "usable": True, "reason": "available", "windows": [{"name": "5h", "kind": "reset_window", "remaining_percent": 73}]}],
    })
    hook.on_selection({
        "provider": "claude", "route": "", "rotation_reason": "rate-limited", "all_rate_limited": True,
        "decision": {"provider": "claude", "usable": False, "reason": "rate-limited"}, "decisions": [],
    })
    hook.on_wait({"provider": "claude", "route": "", "all_rate_limited": True, "decision": {"reason": "rate-limited"}}, 123456)

    events = store.events(mission["id"])
    handoff = next(event for event in events if event["kind"] == "station-handoff")
    selection = next(event for event in events if event["kind"] == "provider-selection")
    waiting = next(event for event in events if event["kind"] == "provider-wait")
    assert handoff["data"]["from"] == "codex-spark"
    assert handoff["data"]["to"] == "claude"
    assert selection["data"]["decisions"][0]["windows"][0]["remaining_percent"] == 73
    assert waiting["data"]["target_epoch"] == 123456


def test_prompt_file_dash_reads_verbatim_stdin(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(read=lambda: "line one\nline two\n"))

    assert bridge.read_verbatim("-") == "line one\nline two\n"


def test_autonomous_station_tail_shows_real_headless_provider_output(tmp_path) -> None:
    import pytest

    from llm_tools.terminal import TmuxTerminalBackend

    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")
    workdir = tmp_path / "repo"
    workdir.mkdir()
    log = tmp_path / "controller.log"
    log.touch(mode=0o600)
    backend = TmuxTerminalBackend()
    handle = backend.start(
        provider_argv=_provider_station_argv(False, log),
        bridge_argv=["/bin/sh", "-c", "sleep 30"],
        cwd=workdir,
        socket_path=tmp_path / "runtime" / "tmux.sock",
        session_name="bridge-feed-test",
    )
    try:
        with log.open("a", encoding="utf-8") as stream:
            stream.write("fake provider output marker\n")
            stream.flush()
        deadline = time.monotonic() + 3
        pane = ""
        while time.monotonic() < deadline:
            pane = backend.capture(handle, pane="provider")
            if "fake provider output marker" in pane:
                break
            time.sleep(0.05)
        assert "fake provider output marker" in pane
    finally:
        backend.stop(handle)


def test_headless_pty_reports_spawned_child_pid_before_wait(tmp_path) -> None:
    identities: list[int] = []
    status, _output = common.run_pty_capture(
        ["python", "-c", "print('child started')"],
        tmp_path,
        5,
        stream=False,
        auto_confirm=False,
        idle_timeout=0,
        question_idle_timeout=0,
        on_process_started=identities.append,
    )

    assert status == 0
    assert len(identities) == 1 and identities[0] > 1


def test_force_stop_checks_persisted_start_token_and_owned_process_group(tmp_path) -> None:
    import os
    import subprocess
    import sys

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        token = _process_start_token(process.pid)
        assert token
        try:
            _terminate_owned_process_group(process.pid, "wrong-start-token", timeout=0.1)
        except PermissionError:
            pass
        else:
            raise AssertionError("must refuse a mismatched process identity")
        assert process.poll() is None
        _terminate_owned_process_group(process.pid, token, timeout=0.5)
        assert process.wait(timeout=2) in {-15, -9}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait()


def test_force_stop_marks_uncertain_then_terminates_only_recorded_headless_group(tmp_path, monkeypatch, capsys) -> None:
    import subprocess
    import sys

    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path, config={"headless": True})
    task = store.add_task(mission["id"], "active task")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task, iteration=1, station="codex")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    store.mark_operation_running(operation, process_pid=process.pid, process_start=_process_start_token(process.pid))
    try:
        assert bridge.main(["stop", "--mission", mission["id"], "--force", "--json"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["mission"]["status"] == "STOPPED"
        assert process.wait(timeout=2) in {-15, -9}
        assert store.get_operation(operation)["status"] == "UNCERTAIN"
        assert store.get_task(task)["status"] == "BLOCKED"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait()


def test_force_stop_refuses_unidentified_headless_process_and_leaves_stopping(tmp_path, monkeypatch, capsys) -> None:
    import subprocess
    import sys

    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path, config={"headless": True})
    task = store.add_task(mission["id"], "active task")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task, iteration=1, station="codex")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    store.mark_operation_running(operation)
    try:
        assert bridge.main(["stop", "--mission", mission["id"], "--force"]) == 2
        assert "no verifiable process identity" in capsys.readouterr().err
        assert process.poll() is None
        assert store.get_mission(mission["id"])["status"] == "STOPPING"
    finally:
        os.killpg(process.pid, 9)
        process.wait()


def test_detached_launch_never_accepts_a_stale_ready_file(tmp_path, monkeypatch) -> None:
    import itertools
    import pytest
    import subprocess
    from types import SimpleNamespace as Namespace

    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path, config={"headless": True})
    runtime = state / "runtime" / f"mission-{mission['id']}"
    runtime.mkdir(parents=True, mode=0o700)
    ready = runtime / "controller-ready.json"
    ready.write_text(json.dumps({"pid": 222, "startup_token": "stale", "status": "STOPPED"}), encoding="utf-8")

    class FakeProcess:
        pid = 333
        returncode = None

        def poll(self):
            return None

    observed = []

    def popen(*args, **kwargs):
        observed.append(not ready.exists())
        return FakeProcess()

    ticks = itertools.count(0, 16)
    monkeypatch.setattr("llm_tools.bridge_runtime.subprocess", Namespace(Popen=popen, DEVNULL=subprocess.DEVNULL))
    monkeypatch.setattr("llm_tools.bridge_runtime.time.monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="did not become ready"):
        launch_controller(mission["id"], headless=True)

    assert observed == [True]


def test_detached_launch_accepts_only_the_new_controller_startup_identity(tmp_path, monkeypatch, capsys) -> None:
    from types import SimpleNamespace as Namespace

    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("launch", "durable objective", cwd=tmp_path, config={"headless": True})
    runtime = state / "runtime" / f"mission-{mission['id']}"
    runtime.mkdir(parents=True, mode=0o700)
    ready = runtime / "controller-ready.json"

    class FakeProcess:
        pid = 4545
        returncode = None

        def poll(self):
            return None

    observed: dict[str, object] = {}

    def popen(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        token = command[command.index("--startup-token") + 1]
        ready.write_text(json.dumps({"pid": 4545, "startup_token": token, "status": "RUNNING"}), encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr("llm_tools.bridge_runtime.subprocess", Namespace(Popen=popen, DEVNULL=subprocess.DEVNULL))
    assert launch_controller(mission["id"], headless=True, providers="codex,claude", routes="fast,review") == 0
    output = capsys.readouterr().out
    assert "controller log" in output
    assert observed["kwargs"]["start_new_session"] is True
    assert "--headless" in observed["command"]
    assert observed["command"][observed["command"].index("--providers") + 1] == "codex,claude"
    assert observed["command"][observed["command"].index("--routes") + 1] == "fast,review"


def test_detached_launch_reports_a_controller_that_exits_before_ready(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace as Namespace

    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("launch failure", "objective", cwd=tmp_path, config={"headless": True})

    class ExitedProcess:
        pid = 4546
        returncode = 9

        def poll(self):
            return self.returncode

    monkeypatch.setattr("llm_tools.bridge_runtime.subprocess", Namespace(Popen=lambda *_args, **_kwargs: ExitedProcess(), DEVNULL=subprocess.DEVNULL))
    with pytest.raises(RuntimeError, match="exited with status 9"):
        launch_controller(mission["id"], headless=True)


def test_detached_native_launch_requires_tmux_before_spawning(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    store = BridgeStore(state / "bridge.sqlite3")
    mission = store.create_mission("native needs tmux", "objective", cwd=tmp_path)
    monkeypatch.setattr("llm_tools.bridge_runtime.shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="tmux is required"):
        launch_controller(mission["id"], native_tui=True)


def test_explicit_native_mode_uses_owned_provider_pane(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    context = {"task_id": task_id}
    handle = SimpleNamespace(session_name="mission-x", socket_path=tmp_path / "tmux.sock", provider_pane="%1")
    hook = BridgeMissionHooks(mission["id"], handle=handle, headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    config = SimpleNamespace(provider="codex", exec_mode="fresh", tmux_target="", tmux_socket="", tmux_pane="", native_tui=False, headless=True, run_dir=None, model="")

    assert hook.before_launch(context, {"provider": "codex"}, config, 1)

    assert config.exec_mode == "tmux"
    assert config.tmux_pane == "%1"
    assert config.native_tui is True
    assert config.headless is False


def _provider_config(provider, cwd, run_dir):
    return SimpleNamespace(
        provider=provider, exec_mode="fresh", tmux_target="", tmux_socket="", tmux_pane="",
        native_tui=False, headless=True, run_dir=run_dir, model="", command_template="", cwd=str(cwd),
    )


def _fake_session_cli(path: Path, provider: str, *, resume: bool = True) -> None:
    options = "--session-id UUID " + ("--resume SESSION_ID" if resume else "")
    path.write_text("#!" + sys.executable + "\nprint(" + repr("usage: " + provider + " " + options) + ")\n", encoding="utf-8")
    path.chmod(0o700)


def _write_provider_metadata(provider: str, home: Path, cwd: Path, session_id: str, *, append: bool = False) -> Path:
    if provider == "claude":
        project = str(cwd.resolve()).replace("/", "-")
        path = home / ".claude" / "projects" / project / f"{session_id}.jsonl"
        record = {"type": "system", "subtype": "init", "session_id": session_id, "cwd": str(cwd.resolve())}
    else:
        path = home / ".copilot" / "session-state" / session_id / "events.jsonl"
        record = {"type": "session.start", "id": "event-id", "data": {
            "sessionId": session_id, "context": {"cwd": str(cwd.resolve())},
        }}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    future_ns = time.time_ns() + 2_000_000
    os.utime(path, ns=(future_ns, future_ns))
    return path


def test_owned_claude_and_copilot_sessions_survive_controller_restart_by_exact_id(tmp_path, monkeypatch) -> None:
    for provider in ("claude", "copilot"):
        case = tmp_path / provider
        case.mkdir(mode=0o700)
        cwd = case / "worktree"
        cwd.mkdir(parents=True)
        home = case / "home"
        home.mkdir()
        bin_dir = case / "bin"
        bin_dir.mkdir()
        binary = bin_dir / provider
        _fake_session_cli(binary, provider)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        store_path = case / "bridge.sqlite3"
        store = BridgeStore(store_path)
        mission = store.create_mission("native", "objective", cwd=cwd)
        store.set_mission_status(mission["id"], "RUNNING")
        hooks = BridgeMissionHooks(mission["id"], handle=SimpleNamespace(session_name="m", socket_path=case / "sock", provider_pane="%1"), headless=False, native_tui=True, controller_logs=case / "controller.log")
        hooks.bind(store)
        task1 = store.add_task(mission["id"], "first prompt")
        config1 = _provider_config(provider, cwd, case / "run1")
        context1 = {"task_id": task1}
        assert hooks.before_launch(context1, {"provider": provider}, config1, 1)
        if provider == "claude":
            initial_argv = config1.command_argv_transform([str(binary), "first prompt"])
        else:
            initial_argv = config1.command_argv_transform([str(binary), "-C", str(cwd), "-i", "first prompt"])
        assert initial_argv[0] == str(binary) and initial_argv[1] == "--session-id"
        session_id = initial_argv[2]
        assert initial_argv.count("--session-id") == 1

        _write_provider_metadata(provider, home, cwd, session_id)
        hooks._record_process(context1["operation_id"], os.getpid())
        assert store.get_operation(context1["operation_id"])["restoration_state"] == "NATIVE_CREATED"
        config1.run_dir.mkdir()
        hooks.after_iteration(context1, {"provider": provider}, config1, 1, 0, 0.1)
        operation1 = store.get_operation(context1["operation_id"])
        assert operation1["native_session_id"] == session_id
        assert operation1["restoration_state"] == "NATIVE_CREATED"
        saved = store.get_mission(mission["id"])["config"]["native_sessions"][provider]
        assert saved["owned_by_bridge"] is True
        assert saved["session_id"] == session_id and saved["cwd"] == str(cwd.resolve())
        store.close()

        # A reopened SQLite connection and new hook instance model controller
        # death/restart; the UUID must come from durable owned state.
        store = BridgeStore(store_path)
        hooks = BridgeMissionHooks(mission["id"], handle=SimpleNamespace(session_name="m", socket_path=case / "sock", provider_pane="%1"), headless=False, native_tui=True, controller_logs=case / "controller.log")
        hooks.bind(store)
        task2 = store.add_task(mission["id"], "resume prompt")
        config2 = _provider_config(provider, cwd, case / "run2")
        context2 = {"task_id": task2}
        assert hooks.before_launch(context2, {"provider": provider}, config2, 2)
        if provider == "claude":
            resumed = config2.command_argv_transform([str(binary), "resume prompt"])
            assert resumed[:3] == [str(binary), "--resume", session_id]
        else:
            resumed = config2.command_argv_transform([str(binary), "-C", str(cwd), "-i", "resume prompt"])
            assert resumed[:3] == [str(binary), "--resume", session_id]
        assert "--last" not in resumed
        assert store.get_operation(context2["operation_id"])["restoration_state"] == "RESTORING"
        assert store.get_operation(context2["operation_id"])["native_session_id"] == session_id
        _write_provider_metadata(provider, home, cwd, session_id, append=True)
        hooks._record_process(context2["operation_id"], os.getpid())
        assert store.get_operation(context2["operation_id"])["restoration_state"] == "RESTORED_NATIVE"
        config2.run_dir.mkdir()
        hooks.after_iteration(context2, {"provider": provider}, config2, 2, 0, 0.1)
        operation2 = store.get_operation(context2["operation_id"])
        assert operation2["native_session_id"] == session_id
        assert operation2["restoration_state"] == "RESTORED_NATIVE"
        store.close()


def test_codex_same_worktree_session_is_never_auto_bound_to_bridge(tmp_path, monkeypatch) -> None:
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!" + sys.executable + "\nprint('codex commands:\\n  resume SESSION_ID [PROMPT]')\n", encoding="utf-8")
    codex.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("native", "objective", cwd=cwd, config={
        "native_sessions": {"codex": {
            "provider": "codex", "session_id": "11111111-1111-4111-8111-111111111111",
            "cwd": str(cwd), "source": "codex:session_meta",
        }}
    })
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "work")
    hooks = BridgeMissionHooks(mission["id"], handle=SimpleNamespace(session_name="m", socket_path=tmp_path / "s", provider_pane="%1"), headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    config = _provider_config("codex", cwd, tmp_path / "run")
    context = {"task_id": task_id}
    assert hooks.before_launch(context, {"provider": "codex"}, config, 1)
    argv = config.command_argv_transform([str(codex), "-C", str(cwd), "work"])
    assert argv == [str(codex), "-C", str(cwd), "work"]

    # Even a unique new session_meta in the same worktree is insufficient
    # provenance: Codex did not let Bridge own this ID.
    unrelated_id = "11111111-1111-4111-8111-111111111111"
    session_dir = home / ".codex" / "sessions" / "2026" / "10" / "25"
    session_dir.mkdir(parents=True)
    (session_dir / f"rollout-{unrelated_id}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": unrelated_id, "cwd": str(cwd)}}) + "\n",
        encoding="utf-8",
    )
    hooks._record_process(context["operation_id"], os.getpid())
    assert store.get_operation(context["operation_id"])["native_session_id"] is None
    config.run_dir.mkdir()
    hooks.after_iteration(context, {"provider": "codex"}, config, 1, 0, 0.1)
    operation = store.get_operation(context["operation_id"])
    assert operation["native_session_id"] is None
    assert operation["restoration_state"] == "UNAVAILABLE"


def test_owned_native_identity_cwd_mismatch_and_capability_drift_do_not_resume(tmp_path, monkeypatch) -> None:
    cwd = tmp_path / "current"
    cwd.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    _fake_session_cli(claude, "claude", resume=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    stale_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    mission = store.create_mission("native", "objective", cwd=cwd, config={
        "native_sessions": {"claude": {
            "owned_by_bridge": True,
            "provider": "claude",
            "session_id": stale_id,
            "cwd": str(other),
            "source": "claude:system.init",
        }}
    })
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "resume from durable checkpoint")
    hooks = BridgeMissionHooks(mission["id"], handle=SimpleNamespace(session_name="m", socket_path=tmp_path / "s", provider_pane="%1"), headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    config = _provider_config("claude", cwd, tmp_path / "run")
    context = {"task_id": task_id}
    assert hooks.before_launch(context, {"provider": "claude"}, config, 1)
    argv = config.command_argv_transform([str(claude), "continue from checkpoint"])

    assert "--resume" not in argv
    assert argv[1] == "--session-id" and argv[2] != stale_id
    config.run_dir.mkdir()
    hooks.after_iteration(context, {"provider": "claude"}, config, 1, 0, 0.1)
    operation = store.get_operation(context["operation_id"])
    assert operation["native_session_id"] is None
    assert operation["restoration_state"] == "UNAVAILABLE"


def test_before_launch_treats_concurrent_mission_stop_as_rejected_launch(tmp_path, monkeypatch) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    store.set_mission_status(mission["id"], "RUNNING")
    task = store.add_task(mission["id"], "work")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    original_begin = store.begin_operation

    def stop_then_begin(task_id, *, iteration, station):
        store.set_mission_status(mission["id"], "STOPPING")
        return original_begin(task_id, iteration=iteration, station=station)

    monkeypatch.setattr(store, "begin_operation", stop_then_begin)
    config = SimpleNamespace(provider="codex", exec_mode="fresh", native_tui=False, headless=True, run_dir=None, model="")

    assert hook.before_launch({"task_id": task}, {"provider": "codex"}, config, 1) is False
    assert store.get_mission(mission["id"])["status"] == "STOPPING"
    assert store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=?", (mission["id"],)).fetchone()[0] == 0
