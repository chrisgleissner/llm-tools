"""Controller boundary behavior for durable Bridge missions."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
import shutil
import time

from llm_tools import bridge
from llm_tools.bridge import _apply_operator_command
from llm_tools import common
from llm_tools.bridge_runtime import BridgeMissionHooks
from llm_tools.bridge_runtime import _provider_station_argv, _process_start_token, _terminate_owned_process_group, launch_controller
from llm_tools.bridge_store import BridgeStore


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
