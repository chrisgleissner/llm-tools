"""Controller boundary behavior for durable Bridge missions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
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
from llm_tools.bridge_store import BridgeStore, MissionLock, TransitionError
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


def test_mission_tmux_socket_skips_an_existing_nonprivate_runtime_directory(tmp_path, monkeypatch) -> None:
    insecure_runtime = tmp_path / "xdg-runtime"
    insecure_socket_dir = insecure_runtime / "llm-bridge"
    insecure_socket_dir.mkdir(parents=True, mode=0o755)
    insecure_socket_dir.chmod(0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(insecure_runtime))

    socket = mission_socket_path("private-fallback-mission")

    assert socket.parent != insecure_socket_dir
    assert socket.parent.stat().st_uid == os.getuid()
    assert socket.parent.stat().st_mode & 0o077 == 0


def test_runtime_and_verifier_directories_refuse_existing_broad_permissions(tmp_path, monkeypatch) -> None:
    from llm_tools.bridge_runtime import _private_verifier_dir, _runtime_directory

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    runtime = _runtime_directory("private-check")
    verifier = _private_verifier_dir(runtime)
    assert runtime.stat().st_mode & 0o777 == 0o700
    assert verifier.stat().st_mode & 0o777 == 0o700
    verifier.chmod(0o755)
    with pytest.raises(PermissionError, match="must be private"):
        _private_verifier_dir(runtime)
    runtime.chmod(0o755)
    with pytest.raises(PermissionError, match="Bridge runtime directory must be private"):
        _runtime_directory("private-check")


def test_bounded_output_digest_returns_none_for_missing_file(tmp_path) -> None:
    from llm_tools.bridge_runtime import _bounded_output_digest

    assert _bounded_output_digest(tmp_path / "removed.log") is None


def test_bounded_output_digest_rejects_path_replaced_between_stat_and_open(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime

    target = tmp_path / "provider.out"
    replacement = tmp_path / "replacement.out"
    target.write_text("provider output", encoding="utf-8")
    replacement.write_text("different file", encoding="utf-8")
    original_open = bridge_runtime.os.open

    def swap_open(path, flags, *args, **kwargs):
        if str(path) == str(target):
            return original_open(replacement, flags, *args, **kwargs)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(bridge_runtime.os, "open", swap_open)

    assert bridge_runtime._bounded_output_digest(target) is None


def test_run_log_pruning_noops_for_disabled_retention_and_missing_directory(tmp_path) -> None:
    from llm_tools import bridge_runtime

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("retention", "objective", cwd=tmp_path)
    root = tmp_path / "runs"
    root.mkdir()
    (root / "old-run").mkdir()

    bridge_runtime._prune_old_run_logs(store, mission["id"], root, retain=0)
    bridge_runtime._prune_old_run_logs(store, mission["id"], root / "absent", retain=1)

    assert (root / "old-run").exists()


def test_run_log_pruning_skips_a_run_directory_that_resolves_outside_log_root(tmp_path, monkeypatch) -> None:
    from pathlib import Path as ConcretePath
    from llm_tools import bridge_runtime

    root = tmp_path / "logs"
    expired = root / "run-expired"
    current = root / "run-current"
    expired.mkdir(parents=True)
    current.mkdir()
    os.utime(expired, (100, 100))
    os.utime(current, (200, 200))
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("retention", "objective", cwd=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_resolve = ConcretePath.resolve

    def escaped_resolution(path, *args, **kwargs):
        if path == expired:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(ConcretePath, "resolve", escaped_resolution)

    bridge_runtime._prune_old_run_logs(store, mission["id"], root, retain=1)

    assert expired.exists()
    assert current.exists()


def test_run_log_pruning_preserves_digest_metadata_when_raw_file_is_already_gone(tmp_path) -> None:
    from llm_tools import bridge_runtime

    log_root = tmp_path / "logs"
    expired = log_root / "run-expired"
    current = log_root / "run-current"
    expired.mkdir(parents=True)
    current.mkdir()
    missing_log = expired / "attempt-1.out"
    os.utime(expired, (100, 100))
    os.utime(current, (200, 200))
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("retention", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id)
    store.finish_operation(operation_id, exit_code=1, logs_path=str(missing_log))

    bridge_runtime._prune_old_run_logs(store, mission["id"], log_root, retain=1)

    event = next(event for event in store.events(mission["id"]) if event["kind"] == "operation-log-pruned")
    assert event["data"]["sha256"] == "unavailable"
    assert event["data"]["size_bytes"] == 0
    assert store.get_operation(operation_id)["logs_path"] is None


def test_managed_process_recovery_does_not_signal_reused_pid(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("managed pid reuse", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation_id, exit_code=0)
    managed_id = store.prepare_managed_process(mission["id"], task_id, kind="verifier")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        actual_token = _process_start_token(process.pid)
        assert actual_token
        store.mark_managed_process_running(managed_id, process_pid=process.pid, process_start="persisted-old-token")
        monkeypatch.setattr(bridge_runtime, "_process_start_token", lambda pid: actual_token if pid == process.pid else "")
        monkeypatch.setattr(bridge_runtime, "_terminate_owned_process_group", lambda *_args, **_kwargs: pytest.fail("reused PID must not be signalled"))

        terminate_managed_processes(store, mission["id"])

        assert process.poll() is None
        assert store.get_managed_process(managed_id)["status"] == "UNCERTAIN"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_managed_process_recovery_skips_a_process_already_marked_exited(tmp_path, monkeypatch) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("already exited verifier", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation_id, exit_code=0)
    managed_id = store.prepare_managed_process(mission["id"], task_id, kind="verifier")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        token = _process_start_token(process.pid)
        assert token
        store.mark_managed_process_running(managed_id, process_pid=process.pid, process_start=token)
        store.finish_managed_process(managed_id, exit_code=0, status="EXITED")
        monkeypatch.setattr(store, "unresolved_managed_processes", lambda _mission: [{"id": managed_id}])
        monkeypatch.setattr("llm_tools.bridge_runtime._process_start_token", lambda _pid: pytest.fail("exited process must not be inspected"))

        terminate_managed_processes(store, mission["id"])

        assert store.get_managed_process(managed_id)["status"] == "EXITED"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_frozen_config_install_is_private_and_sets_child_config_path(tmp_path, monkeypatch) -> None:
    from llm_tools.bridge_runtime import _install_frozen_tool_config

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("LLM_TOOLS_CONFIG", "/old/shared/config.toml")
    _install_frozen_tool_config({"config": {"frozen_tool_config": "[providers.codex]\nmodel='pinned'\n"}}, runtime)

    installed = runtime / "config.toml"
    assert installed.read_text(encoding="utf-8") == "[providers.codex]\nmodel='pinned'\n"
    assert installed.stat().st_mode & 0o777 == 0o600
    assert os.environ["LLM_TOOLS_CONFIG"] == str(installed)


def test_owned_process_tree_rejects_reused_root_identity(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime

    class Entry:
        name = str(os.getpid())

    class Entries:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def __iter__(self): return iter([Entry()])

    monkeypatch.setattr(bridge_runtime.os, "scandir", lambda _path: Entries())
    monkeypatch.setattr(bridge_runtime, "_proc_process_info", lambda pid: {"start": "new-token", "state": "S", "ppid": 1, "pgid": pid})

    with pytest.raises(PermissionError, match="no longer matches"):
        bridge_runtime._owned_process_tree(os.getpid(), "persisted-old-token")


def test_owned_process_tree_fails_closed_if_procfs_cannot_be_scanned(monkeypatch) -> None:
    from llm_tools import bridge_runtime

    monkeypatch.setattr(bridge_runtime.os, "scandir", lambda _path: (_ for _ in ()).throw(PermissionError("no proc access")))

    with pytest.raises(RuntimeError, match="cannot inspect /proc"):
        bridge_runtime._owned_process_tree(10, "token")


def test_owned_process_tree_collects_only_descendants_of_matching_identity(monkeypatch) -> None:
    from llm_tools import bridge_runtime

    class Entry:
        def __init__(self, name): self.name = str(name)

    class Entries:
        def __init__(self, values): self.values = values
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def __iter__(self): return iter(self.values)

    processes = {
        10: {"start": "root", "state": "S", "ppid": 12, "pgid": 10},
        11: {"start": "child", "state": "S", "ppid": 10, "pgid": 10},
        12: {"start": "grandchild", "state": "S", "ppid": 11, "pgid": 12},
        20: {"start": "unrelated", "state": "S", "ppid": 1, "pgid": 20},
    }
    monkeypatch.setattr(bridge_runtime.os, "scandir", lambda _path: Entries(Entry(pid) for pid in processes))
    monkeypatch.setattr(bridge_runtime, "_proc_process_info", lambda pid: processes[int(pid)])

    assert bridge_runtime._owned_process_tree(10, "root") == {pid: processes[pid] for pid in (10, 11, 12)}
    assert bridge_runtime._owned_process_tree(99, "missing") == {}


def test_signal_owned_tree_uses_verified_groups_and_exact_processes(monkeypatch) -> None:
    from llm_tools import bridge_runtime

    signaled_groups: list[tuple[int, int]] = []
    signaled_pids: list[tuple[int, int]] = []
    monkeypatch.setattr(bridge_runtime, "_process_start_token", lambda pid: {10: "leader", 20: "child", 30: "reused"}.get(pid, ""))
    monkeypatch.setattr(bridge_runtime.os, "killpg", lambda pgid, sig: signaled_groups.append((pgid, sig)))
    monkeypatch.setattr(bridge_runtime.os, "kill", lambda pid, sig: signaled_pids.append((pid, sig)))
    snapshot = {
        10: {"start": "leader", "pgid": 10},
        11: {"start": "child-of-group", "pgid": 10},
        20: {"start": "child", "pgid": 1},
        30: {"start": "old-token", "pgid": 1},
    }

    bridge_runtime._signal_owned_process_tree(snapshot, signal.SIGTERM)

    assert signaled_groups == [(10, signal.SIGTERM)]
    assert signaled_pids == [(20, signal.SIGTERM)]


def test_owned_process_group_escalates_when_provider_ignores_sigterm(tmp_path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"],
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2
        token = _process_start_token(process.pid)
        while not token and time.monotonic() < deadline:
            time.sleep(0.01)
            token = _process_start_token(process.pid)
        assert token

        _terminate_owned_process_group(process.pid, token, timeout=0.05)
        assert process.wait(timeout=2) < 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_owned_process_group_refuses_to_claim_success_after_bounded_kill_timeout(monkeypatch) -> None:
    from llm_tools import bridge_runtime

    pid = 12345
    record = {pid: {"start": "owned-token", "state": "S", "ppid": 1, "pgid": pid}}
    ticks = iter([0.0, 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 1.0, 1.1, 1.2])
    monkeypatch.setattr(bridge_runtime, "_process_start_token", lambda process_pid: "owned-token")
    monkeypatch.setattr(bridge_runtime.os, "getpgid", lambda process_pid: pid)
    monkeypatch.setattr(bridge_runtime, "_owned_process_tree", lambda *_args: dict(record))
    monkeypatch.setattr(bridge_runtime, "_signal_owned_process_tree", lambda *_args: None)
    monkeypatch.setattr(bridge_runtime, "_live_process_group_members", lambda _pgid: [pid])
    monkeypatch.setattr(bridge_runtime, "_owned_process_records_alive", lambda _records: True)
    monkeypatch.setattr(bridge_runtime.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(bridge_runtime.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="did not exit after SIGKILL"):
        bridge_runtime._terminate_owned_process_group(pid, "owned-token", timeout=0.05)


def test_process_recording_skips_pid_without_a_verified_start_token(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    store.set_mission_status(mission["id"], "RUNNING")
    task_id = store.add_task(mission["id"], "work")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    monkeypatch.setattr(bridge_runtime, "_process_start_token", lambda _pid: "")

    hook._record_process(operation_id, os.getpid())

    operation = store.get_operation(operation_id)
    assert operation["process_pid"] is None
    assert operation["process_start"] is None


def test_hook_store_property_rejects_unbound_usage(tmp_path) -> None:
    hook = BridgeMissionHooks("mission", handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    with pytest.raises(RuntimeError, match="not bound"):
        _ = hook.store


def test_before_iteration_stops_on_terminal_mission_status(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("failed", "objective", cwd=tmp_path)
    store.set_mission_status(mission["id"], "RUNNING")
    store.set_mission_status(mission["id"], "FAILED")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    assert hook.before_iteration() == {"action": "stop", "reason": "mission failed", "exit_code": 0}


def test_before_iteration_waits_out_mission_pause_and_then_selects_ready_work(tmp_path, monkeypatch) -> None:
    import llm_tools.bridge_runtime as runtime

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("paused", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "resume this work")
    store.set_mission_status(mission["id"], "RUNNING")
    store.set_mission_status(mission["id"], "PAUSED")
    slept: list[float] = []

    def resume(_seconds: float) -> None:
        slept.append(_seconds)
        store.set_mission_status(mission["id"], "RUNNING")

    monkeypatch.setattr(runtime.time, "sleep", resume)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    context = hook.before_iteration()

    assert context["action"] == "run" and context["task_id"] == task_id
    assert slept == [0.5]


def test_before_iteration_waits_when_no_tasks_are_available_then_observes_stop(tmp_path, monkeypatch) -> None:
    import llm_tools.bridge_runtime as runtime

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("empty mission", "objective", cwd=tmp_path)
    store.set_mission_status(mission["id"], "RUNNING")
    sleeps: list[float] = []

    def request_stop(seconds: float) -> None:
        sleeps.append(seconds)
        store.set_mission_status(mission["id"], "FAILED")

    monkeypatch.setattr(runtime.time, "sleep", request_stop)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    result = hook.before_iteration()

    assert sleeps == [0.5]
    assert result["action"] == "stop" and result["reason"] == "mission failed"


def test_selection_without_a_station_is_still_recorded(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("empty selection", "objective", cwd=tmp_path)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    hook.on_selection({"provider": "", "route": "", "rotation_reason": "unavailable", "all_rate_limited": True, "decision": {}, "decisions": []})

    assert any(event["kind"] == "provider-selection" for event in store.events(mission["id"]))


def test_pending_verification_request_stays_queued_until_task_is_verifying(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("mission", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "task")
    store.set_mission_config_value(mission["id"], "verification_requests", [task_id], event_kind="test-request")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    hook._apply_pending_boundary_requests()

    assert store.get_mission(mission["id"])["config"]["verification_requests"] == [task_id]
    assert not any(event["kind"] == "steering-verify-drained" for event in store.events(mission["id"]))


def test_before_iteration_marks_mission_complete_when_every_operator_task_is_verified(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("completed work", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    store.verify_task(task_id, evidence="verified artifact")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    result = hook.before_iteration()

    assert result == {"action": "stop", "reason": "all tasks verified", "exit_code": 0}
    assert store.get_mission(mission["id"])["status"] == "COMPLETED"


def test_handoff_preference_is_preserved_when_no_usable_matching_decision_exists(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("handoff", "objective", cwd=tmp_path)
    store.set_mission_config_value(mission["id"], "handoff_preference", "claude", event_kind="test-handoff")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    malformed = {"decisions": None}
    other_station = {"decisions": [{"provider": "codex", "usable": True}]}

    assert hook.apply_handoff_preference(malformed) is malformed
    assert hook.apply_handoff_preference(other_station) is other_station
    assert store.get_mission(mission["id"])["config"]["handoff_preference"] == "claude"


def test_before_launch_declines_when_mission_is_paused(tmp_path) -> None:
    from types import SimpleNamespace

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("paused", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    store.set_mission_status(mission["id"], "PAUSED")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    allowed = hook.before_launch({"task_id": task_id}, {"provider": "codex"}, SimpleNamespace(provider="codex"), 1)

    assert allowed is False
    assert store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=?", (mission["id"],)).fetchone()[0] == 0


def test_before_launch_rethrows_unrelated_operation_intent_transition_error(tmp_path, monkeypatch) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("intent error", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    monkeypatch.setattr(store, "begin_operation", lambda *_args, **_kwargs: (_ for _ in ()).throw(TransitionError("database transition failure")))

    with pytest.raises(TransitionError, match="database transition failure"):
        hook.before_launch({"task_id": task_id}, {"provider": "codex"}, _provider_config("codex", tmp_path, tmp_path), 1)

    assert store.db.execute("SELECT COUNT(*) FROM operations WHERE mission_id=?", (mission["id"],)).fetchone()[0] == 0


def test_before_launch_rethrows_unrelated_operation_running_transition_error(tmp_path, monkeypatch) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("running error", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    monkeypatch.setattr(store, "mark_operation_running", lambda *_args, **_kwargs: (_ for _ in ()).throw(TransitionError("operation update failed")))

    with pytest.raises(TransitionError, match="operation update failed"):
        hook.before_launch({"task_id": task_id}, {"provider": "codex"}, _provider_config("codex", tmp_path, tmp_path), 1)

    row = store.db.execute("SELECT status FROM operations WHERE mission_id=?", (mission["id"],)).fetchone()
    assert row["status"] == "PREPARED"


def test_before_launch_preserves_unresolvable_configured_cwd_without_claiming_checkpoint(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("unresolved cwd", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    missing_cwd = tmp_path / "removed-worktree"
    config = _provider_config("codex", missing_cwd, tmp_path)
    context = {"task_id": task_id}

    assert hook.before_launch(context, {"provider": "codex"}, config, 1)

    operation_id = context["operation_id"]
    assert hook._session_cwds[operation_id] == str(missing_cwd)
    assert context["repository_before"] is None


def test_wait_interrupted_reports_mission_flags_and_normal_state(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("wait flags", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    assert hook.wait_interrupted() is False
    store.set_mission_config_value(mission["id"], "pause_after_iteration", True, event_kind="test-pause-request")
    assert hook.wait_interrupted() is True
    store.set_mission_config_value(mission["id"], "pause_after_iteration", False, event_kind="test-pause-clear")
    store.set_mission_config_value(mission["id"], "stop_after_task", task_id, event_kind="test-stop-request")
    assert hook.wait_interrupted() is False
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    store.verify_task(task_id, evidence="verified")
    assert hook.wait_interrupted() is True
    store.set_mission_status(mission["id"], "FAILED")
    assert hook.wait_interrupted() is True


def test_boundary_requests_clear_without_replacing_an_existing_pause(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("paused boundary", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    store.verify_task(task_id, evidence="verified")
    store.set_mission_status(mission["id"], "PAUSED")
    store.set_mission_config_values(
        mission["id"], {"stop_after_task": task_id, "pause_after_iteration": True}, event_kind="test-boundary-flags",
    )
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    hook._apply_pending_boundary_requests()

    mission_after_stop = store.get_mission(mission["id"])
    assert mission_after_stop["status"] == "PAUSED"
    assert mission_after_stop["config"].get("stop_after_task") is None
    assert mission_after_stop["config"]["pause_after_iteration"] is True
    hook._apply_pending_boundary_requests()
    mission_after_pause = store.get_mission(mission["id"])
    assert mission_after_pause["status"] == "PAUSED"
    assert mission_after_pause["config"]["pause_after_iteration"] is False


@pytest.mark.parametrize("context", [{}, {"operation_id": 17}, {"operation_id": "missing-operation"}])
def test_after_iteration_ignores_stale_or_malformed_scheduler_callback(tmp_path, context) -> None:
    from types import SimpleNamespace

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("stale callback", "objective", cwd=tmp_path)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    config = SimpleNamespace(run_dir=None, cwd=str(tmp_path), provider="codex")

    assert hook.after_iteration(context, {"provider": "codex"}, config, 1, 0, 0.1) is None
    assert not any(event["kind"] == "operation-progress" for event in store.events(mission["id"]))


def test_after_iteration_ignores_callback_for_already_finished_operation(tmp_path) -> None:
    from types import SimpleNamespace

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("stale callback", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation_id, exit_code=0)
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)

    hook.after_iteration(
        {"operation_id": operation_id}, {"provider": "codex"},
        SimpleNamespace(run_dir=None, cwd=str(tmp_path), provider="codex"), 1, 0, 0.1,
    )

    assert not any(event["kind"] == "operation-progress" for event in store.events(mission["id"]))


def test_owned_codex_observation_persists_only_a_confirmed_exact_identity(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime
    from llm_tools.bridge_sessions import NativeSessionIdentity

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("native", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id, process_pid=os.getpid(), process_start="pid-token")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    hook._codex_owned_operations.add(operation_id)
    hook._session_cwds[operation_id] = str(tmp_path)
    observed = NativeSessionIdentity("codex", "owned-session", str(tmp_path), "codex:writable-session-file")
    monkeypatch.setattr(bridge_runtime, "discover_owned_codex_session", lambda pid, token, *, cwd: observed)
    monkeypatch.setattr(bridge_runtime, "confirm_restored", lambda expected, actual: expected == actual)

    hook._observe_owned_codex_session(operation_id)

    operation = store.get_operation(operation_id)
    assert operation["restoration_state"] == "NATIVE_CREATED"
    assert operation["native_session_id"] == "owned-session"
    assert store.get_mission(mission["id"])["config"]["native_sessions"]["codex"]["cwd"] == str(tmp_path)


def test_owned_codex_observation_rejects_a_mismatched_expected_identity(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime
    from llm_tools.bridge_sessions import NativeSessionIdentity

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("native", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id, process_pid=os.getpid(), process_start="pid-token")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    hook._codex_owned_operations.add(operation_id)
    hook._session_cwds[operation_id] = str(tmp_path)
    hook._session_restore_expectations[operation_id] = NativeSessionIdentity("codex", "expected", str(tmp_path), "resume")
    monkeypatch.setattr(
        bridge_runtime, "discover_owned_codex_session",
        lambda *_args, **_kwargs: NativeSessionIdentity("codex", "unrelated", str(tmp_path), "observed"),
    )

    hook._observe_owned_codex_session(operation_id)

    assert store.get_operation(operation_id)["restoration_state"] == "unknown"


def test_native_capture_without_structured_metadata_records_unavailable(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime
    from llm_tools.bridge_sessions import NativeSessionIdentity

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("native", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="claude")
    store.mark_operation_running(operation_id, process_pid=os.getpid(), process_start="pid-token")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    hook._session_providers[operation_id] = "claude"
    hook._session_cwds[operation_id] = str(tmp_path)
    hook._session_started_ns[operation_id] = 1
    hook._session_candidates[operation_id] = NativeSessionIdentity("claude", "expected", str(tmp_path), "claude:init")
    monkeypatch.setattr(bridge_runtime, "provider_session_metadata", lambda *_args, **_kwargs: None)
    clock = iter([0.0, 13.0])
    monkeypatch.setattr(bridge_runtime.time, "monotonic", lambda: next(clock))

    hook._capture_session_after_launch(operation_id)

    assert store.get_operation(operation_id)["restoration_state"] == "UNAVAILABLE"


def test_native_capture_wait_exits_promptly_when_operator_boundary_interrupts(tmp_path, monkeypatch) -> None:
    from llm_tools import bridge_runtime
    from llm_tools.bridge_sessions import NativeSessionIdentity

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("native", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="claude")
    store.mark_operation_running(operation_id, process_pid=os.getpid(), process_start="pid-token")
    hook = BridgeMissionHooks(mission["id"], handle=None, headless=False, native_tui=True, controller_logs=tmp_path / "controller.log")
    hook.bind(store)
    hook._session_providers[operation_id] = "claude"
    hook._session_cwds[operation_id] = str(tmp_path)
    hook._session_started_ns[operation_id] = 1
    hook._session_candidates[operation_id] = NativeSessionIdentity("claude", "expected", str(tmp_path), "claude:init")
    monkeypatch.setattr(bridge_runtime, "provider_session_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "wait_interrupted", lambda: True)

    hook._capture_session_after_launch(operation_id)

    assert store.get_operation(operation_id)["restoration_state"] == "unknown"


def test_run_log_pruning_keeps_recent_runs_and_digests_removed_operation_logs(tmp_path) -> None:
    from llm_tools import bridge_runtime

    log_root = tmp_path / "logs"
    older = log_root / "run-older"
    recent = log_root / "run-recent"
    older.mkdir(parents=True)
    recent.mkdir()
    expired_output = older / "attempt-1.out"
    expired_output.write_bytes(b"expired provider output\n")
    (recent / "attempt-1.out").write_text("current output\n", encoding="utf-8")
    os.utime(older, (100, 100))
    os.utime(recent, (200, 200))

    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("retention", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id)
    store.finish_operation(operation_id, exit_code=1, logs_path=str(expired_output))

    bridge_runtime._prune_old_run_logs(store, mission["id"], log_root, retain=1)

    assert not older.exists()
    assert recent.exists()
    assert store.get_operation(operation_id)["logs_path"] is None
    event = next(event for event in store.events(mission["id"]) if event["kind"] == "operation-log-pruned")
    assert event["data"]["size_bytes"] == len(b"expired provider output\n")
    assert event["data"]["sha256"] == __import__("hashlib").sha256(b"expired provider output\n").hexdigest()


def test_run_log_pruning_never_follows_an_operation_log_symlink_outside_run(tmp_path) -> None:
    from llm_tools import bridge_runtime

    log_root = tmp_path / "logs"
    older = log_root / "run-older"
    recent = log_root / "run-recent"
    older.mkdir(parents=True)
    recent.mkdir()
    external = tmp_path / "private.out"
    external.write_text("do not read through an escaped log path", encoding="utf-8")
    escaped_log = older / "attempt-1.out"
    escaped_log.symlink_to(external)
    os.utime(older, (100, 100))
    os.utime(recent, (200, 200))
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("retention", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id)
    store.finish_operation(operation_id, exit_code=1, logs_path=str(escaped_log))

    bridge_runtime._prune_old_run_logs(store, mission["id"], log_root, retain=1)

    assert not older.exists()
    assert external.exists()
    assert store.get_operation(operation_id)["logs_path"] == str(escaped_log)
    assert not any(event["kind"] == "operation-log-pruned" for event in store.events(mission["id"]))


@pytest.mark.parametrize("terminal_status", ["COMPLETED", "STOPPING"])
def test_controller_reconciles_terminal_startup_without_running_provider_loop(tmp_path, monkeypatch, terminal_status) -> None:
    from llm_tools.bridge_runtime import _ready_file, _runtime_directory

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("terminal startup", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "finish this")
    store.set_mission_status(mission["id"], "RUNNING")
    if terminal_status == "COMPLETED":
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
        store.verify_task(task_id, evidence="verified")
        store.set_mission_status(mission["id"], "COMPLETED")
    else:
        store.set_mission_status(mission["id"], "STOPPING")
    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", lambda *_args, **_kwargs: pytest.fail("terminal startup must not relaunch provider work"))

    exit_code = run_controller(mission["id"], headless=True, startup_token="terminal-token")

    expected_status = "COMPLETED" if terminal_status == "COMPLETED" else "STOPPED"
    assert exit_code == 0
    assert store.get_mission(mission["id"])["status"] == expected_status
    ready = json.loads(_ready_file(_runtime_directory(mission["id"])).read_text(encoding="utf-8"))
    assert ready["status"] == expected_status
    assert ready["startup_token"] == "terminal-token"


def test_second_controller_returns_without_reentering_owned_mission_lock(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("lock ownership", "objective", cwd=tmp_path)
    lock = MissionLock(tmp_path / "llm-tools" / "llm-bridge" / "locks", mission["id"])
    lock.acquire()
    try:
        assert run_controller(mission["id"], headless=True) == 2
        assert store.get_mission(mission["id"])["status"] == "CREATED"
    finally:
        lock.release()


def test_detached_launch_reports_an_already_owned_controller_without_spawning(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("already owned", "objective", cwd=tmp_path)
    lock = MissionLock(tmp_path / "llm-tools" / "llm-bridge" / "locks", mission["id"])
    lock.acquire()
    monkeypatch.setattr("llm_tools.bridge_runtime.subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("must not spawn a second controller"))
    monkeypatch.setattr("llm_tools.bridge_runtime.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("llm_tools.bridge_runtime.sys.stdout.isatty", lambda: False)
    try:
        assert launch_controller(mission["id"], headless=True) == 2
        assert "already has a controller" in capsys.readouterr().err
        assert store.get_mission(mission["id"])["status"] == "CREATED"
    finally:
        lock.release()


def test_detached_launch_builds_child_environment_and_accepts_matching_readiness(tmp_path, monkeypatch, capsys) -> None:
    from llm_tools.bridge_runtime import _ready_file, _runtime_directory

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission(
        "ready handshake", "objective", cwd=tmp_path, config={"providers": "codex", "routes": "codex-spark"},
    )
    monkeypatch.setattr("llm_tools.bridge_runtime.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("llm_tools.bridge_runtime.sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("llm_tools.bridge_runtime.shutil.which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    captured: dict[str, object] = {}

    class Child:
        pid = 54321
        returncode = None
        def poll(self): return None

    def spawn(argv, *, cwd, env, **kwargs):
        captured.update(argv=argv, cwd=cwd, env=env, kwargs=kwargs)
        token = argv[argv.index("--startup-token") + 1]
        ready = _ready_file(_runtime_directory(mission["id"]))
        ready.write_text(json.dumps({"pid": Child.pid, "startup_token": token, "status": "RUNNING"}), encoding="utf-8")
        return Child()

    monkeypatch.setattr("llm_tools.bridge_runtime.subprocess.Popen", spawn)

    assert launch_controller(mission["id"], headless=True) == 0

    argv = captured["argv"]
    assert argv[argv.index("--providers") + 1] == "codex"
    assert argv[argv.index("--routes") + 1] == "codex-spark"
    assert "--headless" in argv
    assert captured["cwd"] == str(tmp_path)
    assert captured["env"]["LLM_BRIDGE_STATE_DIR"] == str(tmp_path / "llm-tools" / "llm-bridge")
    assert captured["kwargs"]["start_new_session"] is True
    assert "controller log:" in capsys.readouterr().out


def test_controller_launch_rejects_conflicting_headless_native_mode(tmp_path) -> None:
    from llm_tools.bridge_runtime import launch_controller

    with pytest.raises(ValueError, match="cannot be combined"):
        launch_controller("mission", headless=True, native_tui=True)


def test_controller_rejects_conflicting_modes_before_mutating_mission(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("mode conflict", "objective", cwd=tmp_path)

    with pytest.raises(ValueError, match="cannot be combined"):
        run_controller(mission["id"], headless=True, native_tui=True)

    assert store.get_mission(mission["id"])["status"] == "CREATED"


def test_controller_native_mode_without_tmux_fails_truthfully(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("missing tmux", "objective", cwd=tmp_path)
    monkeypatch.setattr("llm_tools.bridge_runtime.shutil.which", lambda name: None if name == "tmux" else "/usr/bin/true")

    with pytest.raises(RuntimeError, match="tmux is required for native mode"):
        run_controller(mission["id"], native_tui=True)

    assert store.get_mission(mission["id"])["status"] == "FAILED"


def test_controller_cleans_owned_station_when_ralph_already_committed_stopped(tmp_path, monkeypatch) -> None:
    from llm_tools.terminal import TerminalHandle

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission(
        "stopped station", "objective", cwd=tmp_path,
        config={"native_tui": True, "providers": "codex", "routes": "codex", "command_template": "fixture --managed"},
    )
    handle = TerminalHandle(tmp_path / "station.sock", tmp_path / "station.conf", "mission-test", "%0", "%1", 6)
    stopped: list[object] = []

    class Backend:
        def start(self, **_kwargs): return handle
        def open_existing(self, **_kwargs): return None
        def stop(self, actual): stopped.append(actual)

    monkeypatch.setattr("llm_tools.bridge_runtime.shutil.which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
    monkeypatch.setattr("llm_tools.bridge_runtime.TmuxTerminalBackend", Backend)
    captured_argv: list[str] = []

    def stop_inside_ralph(argv, *, bridge_hooks):
        captured_argv.extend(argv)
        store.set_mission_status(mission["id"], "STOPPED")
        return 0

    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", stop_inside_ralph)

    assert run_controller(mission["id"], native_tui=True) == 0

    assert captured_argv[captured_argv.index("--routes") + 1] == "codex"
    assert captured_argv[captured_argv.index("--command-template") + 1] == "fixture --managed"
    assert stopped == [handle]


def test_recovery_terminates_a_persisted_verifier_by_start_token(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("managed verifier", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation_id, exit_code=0)
    process_id = store.prepare_managed_process(mission["id"], task_id, kind="verifier")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        start_new_session=True,
    )
    try:
        token = _process_start_token(process.pid)
        assert token
        store.mark_managed_process_running(process_id, process_pid=process.pid, process_start=token)

        terminate_managed_processes(store, mission["id"])

        assert process.wait(timeout=2) < 0
        managed = store.get_managed_process(process_id)
        assert managed["status"] == "TERMINATED"
        assert managed["process_start"] == token
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_controller_restart_rejects_queued_command_without_replaying_its_effect(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    database = tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3"
    store = BridgeStore(database)
    mission = store.create_mission("command recovery", "objective", cwd=tmp_path, config={"headless": True})
    initial_task = store.add_task(mission["id"], "initial")
    store.set_mission_status(mission["id"], "RUNNING")
    command = store.enqueue_command(mission["id"], {"action": "task.add", "prompt": "must not replay"})

    def exit_at_boundary(_argv, *, bridge_hooks):
        store.set_mission_status(mission["id"], "STOPPING")
        return 0

    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", exit_at_boundary)

    assert run_controller(mission["id"], headless=True) == 0

    assert len(store.list_tasks(mission["id"])) == 1
    assert store.get_task(initial_task)["prompt"] == "initial"
    recovered = store.get_command(command["id"])
    assert recovered["status"] == "REJECTED"
    assert "was not replayed" in recovered["result"]["error"]


def test_headless_restart_marks_provider_operation_uncertain_and_kills_verified_process(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("headless recovery", "objective", cwd=tmp_path, config={"headless": True})
    task_id = store.add_task(mission["id"], "mutating task")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        token = _process_start_token(process.pid)
        assert token
        store.mark_operation_running(operation_id, process_pid=process.pid, process_start=token)

        def exit_after_recovery(_argv, *, bridge_hooks):
            assert store.get_operation(operation_id)["status"] == "UNCERTAIN"
            assert store.get_task(task_id)["status"] == "BLOCKED"
            assert process.wait(timeout=2) < 0
            store.set_mission_status(mission["id"], "STOPPING")
            return 0

        monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", exit_after_recovery)

        assert run_controller(mission["id"], headless=True) == 0
        assert store.get_mission(mission["id"])["status"] == "STOPPED"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_headless_restart_refuses_running_operation_without_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("uncertain recovery", "objective", cwd=tmp_path, config={"headless": True})
    task_id = store.add_task(mission["id"], "unidentified work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    store.mark_operation_running(operation_id)
    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", lambda *_args, **_kwargs: pytest.fail("must not replay unidentified operation"))

    with pytest.raises(RuntimeError, match="cannot prove identity"):
        run_controller(mission["id"], headless=True)

    assert store.get_operation(operation_id)["status"] == "UNCERTAIN"
    assert store.get_task(task_id)["status"] == "BLOCKED"


def test_headless_restart_refuses_reused_provider_pid_without_signalling(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("LLM_BRIDGE_STATE_DIR", raising=False)
    store = BridgeStore(tmp_path / "llm-tools" / "llm-bridge" / "bridge.sqlite3")
    mission = store.create_mission("reused provider pid", "objective", cwd=tmp_path, config={"headless": True})
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation_id = store.begin_operation(task_id, iteration=1, station="codex")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        actual_token = _process_start_token(process.pid)
        assert actual_token
        store.mark_operation_running(operation_id, process_pid=process.pid, process_start="persisted-old-token")
        monkeypatch.setattr(
            "llm_tools.bridge_runtime._process_start_token",
            lambda pid: actual_token if int(pid) == process.pid else "",
        )
        monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", lambda *_args, **_kwargs: pytest.fail("must not schedule around ambiguous provider"))

        with pytest.raises(PermissionError, match="belongs to a different process"):
            run_controller(mission["id"], headless=True)

        assert process.poll() is None
        assert store.get_operation(operation_id)["status"] == "UNCERTAIN"
        assert store.get_task(task_id)["status"] == "BLOCKED"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


@pytest.mark.parametrize(
    ("kill_error", "expected"),
    [(ProcessLookupError(), False), (PermissionError(), True)],
)
def test_unreadable_process_identity_only_counts_alive_after_kill_probe(tmp_path, monkeypatch, kill_error, expected) -> None:
    from pathlib import Path as ConcretePath
    from llm_tools import bridge_runtime

    original_read_text = ConcretePath.read_text

    def unreadable_proc_stat(path, *args, **kwargs):
        if str(path) == "/proc/424242/stat":
            raise PermissionError("proc identity is hidden")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(ConcretePath, "read_text", unreadable_proc_stat)
    monkeypatch.setattr(bridge_runtime.os, "kill", lambda _pid, _sig: (_ for _ in ()).throw(kill_error))

    assert bridge_runtime._process_alive_but_unverifiable(424242) is expected


def test_unverifiable_process_identity_with_successful_signal_zero_is_alive(tmp_path, monkeypatch) -> None:
    from pathlib import Path as ConcretePath
    from llm_tools import bridge_runtime

    original_read_text = ConcretePath.read_text

    def unreadable_proc_stat(path, *args, **kwargs):
        if str(path) == "/proc/424242/stat":
            raise PermissionError("proc identity is hidden")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(ConcretePath, "read_text", unreadable_proc_stat)
    monkeypatch.setattr(bridge_runtime.os, "kill", lambda _pid, _sig: None)

    assert bridge_runtime._process_alive_but_unverifiable(424242) is True


def test_process_identity_probe_reports_missing_proc_entry_as_not_alive(tmp_path, monkeypatch) -> None:
    from pathlib import Path as ConcretePath
    from llm_tools import bridge_runtime

    original_read_text = ConcretePath.read_text

    def missing_proc_stat(path, *args, **kwargs):
        if str(path) == "/proc/424242/stat":
            raise FileNotFoundError(str(path))
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(ConcretePath, "read_text", missing_proc_stat)

    assert bridge_runtime._process_alive_but_unverifiable(424242) is False


def test_proc_process_info_returns_none_for_missing_or_malformed_process_stat(tmp_path, monkeypatch) -> None:
    from pathlib import Path as ConcretePath
    from llm_tools import bridge_runtime

    assert bridge_runtime._proc_process_info(999999999) is None
    original_read_text = ConcretePath.read_text

    def malformed_proc_stat(path, *args, **kwargs):
        if str(path) == "/proc/424242/stat":
            return "424242 (broken) S 1"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(ConcretePath, "read_text", malformed_proc_stat)
    assert bridge_runtime._proc_process_info(424242) is None


def test_process_group_inspection_fails_closed_when_procfs_is_unavailable(monkeypatch) -> None:
    from llm_tools import bridge_runtime

    monkeypatch.setattr(bridge_runtime.os, "scandir", lambda _path: (_ for _ in ()).throw(OSError("procfs unavailable")))

    assert bridge_runtime._live_process_group_members(424242) == [424242]


def test_live_process_group_listing_ignores_malformed_proc_entries(monkeypatch) -> None:
    from pathlib import Path as ConcretePath
    from llm_tools import bridge_runtime

    class Entry:
        def __init__(self, name):
            self.name = str(name)
            self.path = f"/proc/{name}"

    class Entries:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def __iter__(self): return iter([Entry(101), Entry(102), Entry("bad")])

    original_read_text = ConcretePath.read_text

    def read_stat(path, *args, **kwargs):
        if str(path) == "/proc/101/stat":
            return "101 (member) S 1 100 100"
        if str(path) == "/proc/102/stat":
            return "102 (zombie) Z 1 100 100"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(bridge_runtime.os, "scandir", lambda _path: Entries())
    monkeypatch.setattr(ConcretePath, "read_text", read_stat)

    assert bridge_runtime._live_process_group_members(100) == [101]


def test_signal_tree_falls_back_to_exact_pid_if_group_disappears(monkeypatch) -> None:
    from llm_tools import bridge_runtime

    pids: list[int] = []
    monkeypatch.setattr(bridge_runtime, "_process_start_token", lambda pid: f"token-{pid}")
    monkeypatch.setattr(
        bridge_runtime.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError("group already exited")),
    )
    monkeypatch.setattr(bridge_runtime.os, "kill", lambda pid, _sig: pids.append(pid))

    bridge_runtime._signal_owned_process_tree(
        {10: {"start": "token-10", "pgid": 10}, 11: {"start": "token-11", "pgid": 10}},
        signal.SIGTERM,
    )

    assert pids == [10, 11]


def test_owned_group_termination_refuses_nonleader_and_returns_for_vanished_pid(monkeypatch) -> None:
    from llm_tools import bridge_runtime

    monkeypatch.setattr(bridge_runtime, "_process_start_token", lambda _pid: "identity")
    monkeypatch.setattr(bridge_runtime.os, "getpgid", lambda _pid: 1)
    with pytest.raises(PermissionError, match="not a process-group leader"):
        bridge_runtime._terminate_owned_process_group(22, "identity")

    monkeypatch.setattr(bridge_runtime.os, "getpgid", lambda _pid: (_ for _ in ()).throw(ProcessLookupError()))
    assert bridge_runtime._terminate_owned_process_group(22, "identity") is None


def test_owned_group_termination_returns_when_verified_tree_already_disappeared(monkeypatch) -> None:
    from llm_tools import bridge_runtime

    monkeypatch.setattr(bridge_runtime, "_process_start_token", lambda _pid: "identity")
    monkeypatch.setattr(bridge_runtime.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(bridge_runtime, "_owned_process_tree", lambda *_args: {})

    assert bridge_runtime._terminate_owned_process_group(22, "identity") is None


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


@pytest.mark.parametrize("mission_status", ["PAUSED", "STOPPING", "STOPPED", "CANCELLED", "COMPLETED", "FAILED"])
def test_wait_interrupts_for_every_terminal_or_paused_mission_state(tmp_path, mission_status) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("wait interrupt", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    operation = store.begin_operation(task_id, iteration=1, station="codex")
    store.finish_operation(operation, exit_code=0)
    store.verify_task(task_id, approved=True)
    store.set_mission_status(mission["id"], mission_status)
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    assert hooks._should_interrupt_wait(task_id) is True


@pytest.mark.parametrize("task_status", ["PAUSED", "CANCELLED"])
def test_wait_interrupts_for_task_pause_and_cancel(tmp_path, task_status) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("task wait interrupt", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    store.set_task_status(task_id, task_status)
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    assert hooks._should_interrupt_wait(task_id) is True


def test_wait_interrupts_on_boundary_flags_and_returns_false_during_normal_work(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("boundary wait", "objective", cwd=tmp_path)
    task_id = store.add_task(mission["id"], "work")
    store.set_mission_status(mission["id"], "RUNNING")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    assert hooks._should_interrupt_wait(task_id) is False
    store.set_mission_config_value(mission["id"], "pause_after_iteration", True, event_kind="test")
    assert hooks._should_interrupt_wait(task_id) is True
    store.set_mission_config_value(mission["id"], "pause_after_iteration", False, event_kind="test")
    completed = store.add_task(mission["id"], "stop after this")
    store.set_task_status(completed, "READY")
    with store._transaction():
        store.db.execute("UPDATE tasks SET status='COMPLETED' WHERE id=?", (completed,))
    store.set_mission_config_value(mission["id"], "stop_after_task", completed, event_kind="test")
    assert hooks._should_interrupt_wait(task_id) is True


def test_handoff_preference_ignores_unusable_entries_and_consumes_first_usable_choice(tmp_path) -> None:
    store = BridgeStore(tmp_path / "bridge.sqlite3")
    mission = store.create_mission("handoff", "objective", cwd=tmp_path, config={"providers": "codex,claude"})
    store.set_mission_config_value(mission["id"], "handoff_preference", "claude", event_kind="test")
    hooks = BridgeMissionHooks(mission["id"], handle=None, headless=True, controller_logs=tmp_path / "controller.log")
    hooks.bind(store)
    selection = {
        "provider": "codex", "index": 0, "decisions": [
            None, {"provider": "codex", "usable": False},
            {"provider": "claude", "route": "claude", "usable": True},
        ],
    }
    updated = hooks.apply_handoff_preference(selection)
    assert updated["provider"] == "claude" and updated["index"] == 2
    assert updated["rotation_reason"] == "operator-handoff"
    assert store.get_mission(mission["id"])["config"]["handoff_preference"] == ""
    assert hooks.apply_handoff_preference(selection) == selection
    malformed = {"provider": "codex", "decisions": "not-a-list"}
    assert hooks.apply_handoff_preference(malformed) == malformed


def test_verifier_artifact_records_are_relative_bounded_and_fail_closed(tmp_path) -> None:
    import hashlib
    from llm_tools.bridge_runtime import _artifact_records

    root = tmp_path / "work"
    root.mkdir()
    artifact = root / "result.bin"
    artifact.write_bytes(b"evidence")
    assert _artifact_records(root, ["result.bin"]) == [{
        "path": "result.bin", "sha256": hashlib.sha256(b"evidence").hexdigest(), "size": "8",
    }]
    directory = root / "folder"
    directory.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "outside-link"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="non-empty strings"):
        _artifact_records(root, [""])
    with pytest.raises(ValueError, match="100 paths"):
        _artifact_records(root, ["result.bin"] * 101)
    with pytest.raises(ValueError, match="outside"):
        _artifact_records(root, [str(outside)])
    with pytest.raises(ValueError, match="outside"):
        _artifact_records(root, ["outside-link"])
    with pytest.raises(ValueError, match="regular file"):
        _artifact_records(root, ["folder"])
    with pytest.raises(FileNotFoundError):
        _artifact_records(root, ["missing"])
    too_large = root / "large.bin"
    with too_large.open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="exceeds 50 MiB"):
        _artifact_records(root, ["large.bin"])


def test_bounded_verifier_process_callbacks_report_identity_and_exit(tmp_path) -> None:
    from llm_tools.bridge_runtime import _run_bounded_verifier

    started: list[tuple[int, str]] = []
    finished: list[int] = []
    log = tmp_path / "verifier.log"
    result = _run_bounded_verifier(
        [sys.executable, "-c", "print('structured check')"],
        str(tmp_path),
        2,
        log,
        on_started=lambda pid, token: started.append((pid, token)),
        on_finished=finished.append,
    )
    assert result["exit_code"] == 0 and not result["timed_out"]
    assert result["output"].strip() == b"structured check"
    assert len(started) == 1 and started[0][0] > 1 and started[0][1]
    assert finished == [0]
    assert log.stat().st_mode & 0o777 == 0o600


def test_verifier_launch_identity_failure_still_runs_finished_cleanup(tmp_path, monkeypatch) -> None:
    from llm_tools.bridge_runtime import _run_bounded_verifier

    finished: list[int] = []
    monkeypatch.setattr("llm_tools.bridge_runtime._process_start_token", lambda _pid: "")
    with pytest.raises(RuntimeError, match="verify verifier process identity"):
        _run_bounded_verifier(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            str(tmp_path),
            2,
            tmp_path / "verifier.log",
            on_started=lambda _pid, _token: None,
            on_finished=finished.append,
        )
    assert finished and finished[0] in {-signal.SIGTERM, -signal.SIGKILL}


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


@pytest.mark.parametrize("controller_error", [False, True])
def test_run_controller_records_ready_and_finalizes_completion_or_failure(tmp_path, monkeypatch, capsys, controller_error) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("LLM_BRIDGE_STATE_DIR", str(state))
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission(
            "controller path", "objective", cwd=tmp_path,
            config={"headless": True, "frozen_tool_config": "[ralph]\nproviders = ['codex']\n"},
        )
        mission_id = mission["id"]
        task_id = store.add_task(mission_id, "verified initial task")
        store.set_mission_status(mission_id, "RUNNING")
        operation = store.begin_operation(task_id, iteration=1, station="codex")
        store.finish_operation(operation, exit_code=0)
        store.verify_task(task_id, approved=True)
        store.set_mission_status(mission_id, "STOPPED")

    worker_events: list[str] = []
    class FakeWorker:
        def __init__(self, *_args, **_kwargs): pass
        def start(self): worker_events.append("started")
        def stop(self): worker_events.append("stopped")

    monkeypatch.setattr("llm_tools.bridge_runtime.CapacityRefreshWorker", FakeWorker)

    def fake_ralph(_argv, *, bridge_hooks):
        assert bridge_hooks.mission_id == mission_id
        if controller_error:
            raise RuntimeError("simulated loop failure")
        result = bridge_hooks.before_iteration()
        assert result["action"] == "stop"
        return 0

    monkeypatch.setattr("llm_tools.bridge_runtime.ralph_robin.main", fake_ralph)
    result = run_controller(mission_id, headless=True, startup_token="test-start")
    expected_status = "FAILED" if controller_error else "COMPLETED"
    assert result == (1 if controller_error else 0)
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission_id)["status"] == expected_status
        events = store.events(mission_id)
    assert worker_events == ["started", "stopped"]
    assert any(event["kind"] == "controller-mode" for event in events)
    assert (state / "runtime" / f"mission-{mission_id}" / "controller-ready.json").is_file()
    if controller_error:
        assert "simulated loop failure" in capsys.readouterr().err


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
        import re
        project = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
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
