"""Real tmux station recovery behavior without live provider accounts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

from llm_tools.bridge_store import BridgeStore, MissionLock
from llm_tools.terminal import TmuxTerminalBackend


def call(env: dict[str, str], *args: object) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def wait_for(predicate, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "Bridge recovery condition was not reached"


def controller_released(state: Path, mission_id: str) -> bool:
    lock = MissionLock(state / "locks", mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        return False
    lock.release()
    return True


def _mission_task_state(database: Path, mission_id: str, task_id: int) -> tuple[str, str]:
    with BridgeStore(database) as store:
        return store.get_mission(mission_id)["status"], store.get_task(task_id)["status"]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        if state[state.rfind(")") + 2 :].split()[0] == "Z":
            return False
    except OSError:
        pass
    return True


def test_graceful_stop_terminates_tmux_station_and_bridge_ui(tmp_path: Path, env: dict[str, str]) -> None:
    assert shutil.which("tmux"), "CI must install tmux for Bridge station integration tests"
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps(
        {"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}}
    )
    provider = tmp_path / "provider.py"
    provider.write_text(
        "from pathlib import Path\n"
        "Path('provider-ran').write_text('yes')\n"
        "print('native fixture completed', flush=True)\n",
        encoding="utf-8",
    )
    template = shlex.join([sys.executable, str(provider)])
    created = json.loads(
        call(
            env,
            "mission",
            "create",
            "--prompt",
            "Finish once, then await verification",
            "--cwd",
            tmp_path,
            "--providers",
            "codex",
            "--native",
            "--command-template",
            template,
            "--json",
        )
    )
    mission_id = created["mission"]["id"]
    task_id = int(created["task"]["id"])
    database = state / "bridge.sqlite3"
    runtime = state / "runtime" / f"mission-{mission_id}"
    backend = TmuxTerminalBackend()
    handle = None
    try:
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: (tmp_path / "provider-ran").exists())
        wait_for(lambda: _mission_task_state(database, mission_id, task_id)[1] == "VERIFYING")
        handle = backend.open_existing(runtime / "tmux.sock", f"mission-{mission_id}")
        assert backend.session_exists(handle)
        assert backend.pane_is_alive(handle, pane="bridge")

        call(env, "stop", "--mission", mission_id)
        wait_for(lambda: _mission_task_state(database, mission_id, task_id)[0] == "STOPPED")
        wait_for(lambda: controller_released(state, mission_id))

        assert not backend.session_exists(handle), "graceful stop must terminate the managed station"
        assert not backend.pane_is_alive(handle, pane="bridge"), "graceful stop must terminate the managed UI"
        with BridgeStore(database) as store:
            assert store.get_task(task_id)["status"] == "VERIFYING"
    finally:
        if handle is None and (runtime / "tmux.sock").exists():
            try:
                handle = backend.open_existing(runtime / "tmux.sock", f"mission-{mission_id}")
            except (OSError, RuntimeError, PermissionError):
                handle = None
        if handle is not None:
            backend.stop(handle)


def test_resume_reconciles_native_mutator_and_stops_old_station(tmp_path: Path, env: dict[str, str]) -> None:
    assert shutil.which("tmux"), "CI must install tmux for Bridge station integration tests"
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps(
        {"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}}
    )
    pid_file = tmp_path / "provider.pid"
    beat_file = tmp_path / "provider.heartbeat"
    provider = tmp_path / "provider.py"
    provider.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        f"pid_file = Path({str(pid_file)!r})\n"
        f"beat_file = Path({str(beat_file)!r})\n"
        "pid_file.write_text(str(os.getpid()))\n"
        "print('native mutator active', flush=True)\n"
        "while True:\n"
        "    beat_file.write_text(str(time.monotonic_ns()))\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    template = shlex.join([sys.executable, str(provider)])
    created = json.loads(
        call(
            env,
            "mission",
            "create",
            "--prompt",
            "Long running native mutation",
            "--cwd",
            tmp_path,
            "--providers",
            "codex",
            "--native",
            "--command-template",
            template,
            "--json",
        )
    )
    mission_id = created["mission"]["id"]
    task_id = int(created["task"]["id"])
    database = state / "bridge.sqlite3"
    runtime = state / "runtime" / f"mission-{mission_id}"
    backend = TmuxTerminalBackend()
    old_handle = None
    try:
        call(env, "start", "--mission", mission_id)
        wait_for(lambda: pid_file.exists() and beat_file.exists())
        provider_pid = int(pid_file.read_text(encoding="ascii"))
        assert _pid_alive(provider_pid)
        old_handle = backend.open_existing(runtime / "tmux.sock", f"mission-{mission_id}")
        assert backend.pane_is_alive(old_handle, pane="provider")

        ready = json.loads((runtime / "controller-ready.json").read_text(encoding="utf-8"))
        controller_pid = int(ready["pid"])
        os.kill(controller_pid, signal.SIGKILL)
        wait_for(lambda: controller_released(state, mission_id))
        heartbeat_after_crash = beat_file.read_text(encoding="ascii")
        time.sleep(0.15)
        assert beat_file.read_text(encoding="ascii") != heartbeat_after_crash
        assert _pid_alive(provider_pid), "fixture must prove the provider survived controller death"

        call(env, "resume", "--mission", mission_id)
        wait_for(lambda: _mission_task_state(database, mission_id, task_id)[1] == "BLOCKED")
        wait_for(lambda: not _pid_alive(provider_pid))
        mission_status, task_status = _mission_task_state(database, mission_id, task_id)
        assert mission_status == "RUNNING"
        assert task_status == "BLOCKED"
        with BridgeStore(database) as store:
            operations = store.db.execute(
                "SELECT status FROM operations WHERE mission_id=? ORDER BY created_at",
                (mission_id,),
            ).fetchall()
        assert [row["status"] for row in operations] == ["UNCERTAIN"]

        resumed_handle = backend.open_existing(runtime / "tmux.sock", f"mission-{mission_id}")
        assert backend.session_exists(resumed_handle)
        assert backend.pane_is_alive(resumed_handle, pane="bridge")
        assert not backend.pane_is_alive(resumed_handle, pane="provider")
    finally:
        if old_handle is None and (runtime / "tmux.sock").exists():
            try:
                old_handle = backend.open_existing(runtime / "tmux.sock", f"mission-{mission_id}")
            except (OSError, RuntimeError, PermissionError):
                old_handle = None
        provider_pid = int(pid_file.read_text(encoding="ascii")) if pid_file.exists() else -1
        if provider_pid > 1 and _pid_alive(provider_pid):
            try:
                os.kill(provider_pid, signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        if old_handle is not None:
            backend.stop(old_handle)
