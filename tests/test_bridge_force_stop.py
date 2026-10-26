"""Force stop must prove and terminate the actual controller owner."""

from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import sys
import time
from pathlib import Path

from llm_tools.bridge_runtime import _process_start_token
from llm_tools.bridge_store import BridgeStore, MissionLock


def _call(env: dict[str, str], *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
        env=env, text=True, capture_output=True, timeout=8,
    )


def _wait(predicate, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "controller force-stop state was not reached"


def _controller_locked(state: Path, mission_id: str) -> bool:
    lock = MissionLock(state / "locks", mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        return True
    lock.release()
    return False


def _mission_status(database: Path, mission_id: str) -> str:
    with BridgeStore(database) as store:
        return str(store.get_mission(mission_id)["status"])


def test_force_stop_refuses_stale_ready_pid_while_real_controller_owns_lock(tmp_path: Path, env: dict[str, str]) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "wait without provider process", cwd=tmp_path, config={"headless": True})
        mission_id = str(mission["id"])
        store.set_mission_status(mission_id, "RUNNING")
    runtime = state / "runtime" / f"mission-{mission_id}"
    runtime.mkdir(mode=0o700, parents=True)
    ready = runtime / "controller-ready.json"
    database = state / "bridge.sqlite3"
    foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    owner_code = (
        "import sys,time; from pathlib import Path; from llm_tools.bridge_store import MissionLock; "
        "lock=MissionLock(Path(sys.argv[1]),sys.argv[2]); lock.acquire(); "
        "Path(sys.argv[3]).write_text('ready'); time.sleep(30)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(state / "locks"), mission_id, str(tmp_path / "owner.ready")],
        env=env, start_new_session=True,
    )
    try:
        _wait(lambda: (tmp_path / "owner.ready").exists() and _controller_locked(state, mission_id))

        # A valid but unrelated live PID in the ready file must never be used
        # to claim the mission controller has stopped.
        ready.write_text(json.dumps({
            "pid": foreign.pid,
            "process_start": "stale-process-start-token",
            "startup_token": "stale-ready-token",
            "status": "RUNNING",
        }), encoding="utf-8")
        stopped = _call(env, "stop", "--mission", mission_id, "--force")

        assert stopped.returncode == 2
        assert "controller PID no longer matches" in stopped.stderr
        assert _mission_status(database, mission_id) == "STOPPING"
        assert _controller_locked(state, mission_id)
        assert foreign.poll() is None

    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGTERM)
            owner.wait(timeout=3)
        if foreign.poll() is None:
            os.killpg(foreign.pid, signal.SIGTERM)
            foreign.wait(timeout=3)


def test_force_stop_terminates_matching_controller_before_marking_stopped(tmp_path: Path, env: dict[str, str]) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    with BridgeStore(state / "bridge.sqlite3") as store:
        mission = store.create_mission("mission", "controller identity", cwd=tmp_path)
        mission_id = str(mission["id"])
        store.set_mission_status(mission_id, "RUNNING")
    runtime = state / "runtime" / f"mission-{mission_id}"
    runtime.mkdir(mode=0o700, parents=True)
    ready = runtime / "controller-ready.json"
    owner_code = (
        "import sys,time; from pathlib import Path; from llm_tools.bridge_store import MissionLock; "
        "lock=MissionLock(Path(sys.argv[1]),sys.argv[2]); lock.acquire(); "
        "Path(sys.argv[3]).write_text('ready'); time.sleep(30)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(state / "locks"), mission_id, str(tmp_path / "owner.ready")],
        env=env, start_new_session=True,
    )
    try:
        _wait(lambda: (tmp_path / "owner.ready").exists() and _controller_locked(state, mission_id))
        ready.write_text(json.dumps({
            "pid": owner.pid,
            "process_start": _process_start_token(owner.pid),
            "startup_token": "validated-test-controller",
            "status": "RUNNING",
        }), encoding="utf-8")

        stopped = _call(env, "stop", "--mission", mission_id, "--force")

        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert _mission_status(state / "bridge.sqlite3", mission_id) == "STOPPED"
        assert owner.poll() is not None
        assert not _controller_locked(state, mission_id)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGTERM)
            owner.wait(timeout=3)


def test_force_stop_terminates_owned_verifier_before_controller(tmp_path: Path, env: dict[str, str]) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps(
        {"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}}
    )
    verifier_pid_file = tmp_path / "verifier.pid"
    heartbeat = tmp_path / "verifier.heartbeat"
    verifier = tmp_path / "verifier.py"
    verifier.write_text(
        "import os,time\n"
        "from pathlib import Path\n"
        f"Path({str(verifier_pid_file)!r}).write_text(str(os.getpid()))\n"
        f"path=Path({str(heartbeat)!r})\n"
        "while True:\n"
        " path.write_text(str(time.monotonic_ns()))\n"
        " time.sleep(.05)\n",
        encoding="utf-8",
    )
    created = _call(
        env,
        "mission", "create", "--prompt", "verify a long-running fixture", "--cwd", tmp_path,
        "--headless", "--providers", "codex",
        "--command-template", shlex.join([sys.executable, "-c", "print('provider complete')"]),
        "--verifier-arg", sys.executable, "--verifier-arg", str(verifier),
        "--verifier-timeout", "300", "--json",
    )
    assert created.returncode == 0, created.stderr
    mission_id = json.loads(created.stdout)["mission"]["id"]
    database = state / "bridge.sqlite3"
    try:
        started = _call(env, "start", "--mission", mission_id)
        assert started.returncode == 0, started.stderr + started.stdout
        _wait(lambda: verifier_pid_file.exists() and heartbeat.exists())
        verifier_pid = int(verifier_pid_file.read_text(encoding="ascii"))
        assert _process_alive(verifier_pid)

        stopped = _call(env, "stop", "--mission", mission_id, "--force")

        assert stopped.returncode == 0, stopped.stderr + stopped.stdout
        _wait(lambda: not _process_alive(verifier_pid))
        assert _mission_status(database, mission_id) == "STOPPED"
        assert not _controller_locked(state, mission_id)
        with BridgeStore(database) as store:
            assert store.unresolved_managed_processes(mission_id) == []
            managed = store.db.execute("SELECT status FROM managed_processes WHERE mission_id=?", (mission_id,)).fetchall()
            tasks = store.list_tasks(mission_id)
        assert [row["status"] for row in managed] in (["TERMINATED"], ["EXITED"])
        assert tasks[0]["status"] == "VERIFYING", "forced verifier termination must not create verification evidence"
        last_heartbeat = heartbeat.read_text(encoding="ascii")
        time.sleep(0.15)
        assert heartbeat.read_text(encoding="ascii") == last_heartbeat
    finally:
        if verifier_pid_file.exists():
            verifier_pid = int(verifier_pid_file.read_text(encoding="ascii"))
            if _process_alive(verifier_pid):
                try:
                    os.killpg(verifier_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        return status[status.rfind(")") + 2:].split()[0] != "Z"
    except OSError:
        return True
