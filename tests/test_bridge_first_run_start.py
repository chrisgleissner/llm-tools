"""`start` from a script says how to reach the work (real controller, fake provider)."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.bridge_store import BridgeStore, MissionLock
from tests.conftest import scaled, wait_for as _shared_wait_for


def _call(env, *args) -> str:
    result = subprocess.run([sys.executable, "-m", "llm_tools.bridge", *map(str, args)], env=env, text=True, capture_output=True, timeout=scaled(30))
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def _wait(predicate, timeout=15.0):
    return _shared_wait_for(predicate, timeout, 0.05, message="condition not reached")


def _released(state: Path, mission_id: str) -> bool:
    lock = MissionLock(state / "locks", mission_id)
    try:
        lock.acquire()
    except BlockingIOError:
        return False
    lock.release()
    return True


def _mission(env, tmp_path: Path, *mode: str) -> tuple[str, Path]:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}})
    provider = tmp_path / "provider.py"
    # Stay alive after stdin closes (a headless provider has none): the tests stop the mission themselves,
    # and a provider that already exited makes `stop --force` race the controller's own cleanup.
    provider.write_text("import sys, time\nprint('fake provider ready', flush=True)\nfor line in sys.stdin:\n    if line.strip() == '/quit':\n        break\ntime.sleep(60)\n")
    created = json.loads(_call(env, "mission", "create", "--prompt", "Start message", "--cwd", tmp_path, "--providers", "codex",
                               *mode, "--command-template", shlex.join([sys.executable, str(provider)]), "--json"))
    return created["mission"]["id"], state


def test_native_start_from_a_script_ends_with_the_attach_command(tmp_path, env) -> None:
    assert shutil.which("tmux")
    mission_id, state = _mission(env, tmp_path, "--native")
    short = mission_id[:8]
    socket = mission_socket_path(mission_id, env=env)
    try:
        out = _call(env, "start", "--mission", mission_id)
        assert f"mission {short} running · attach with: llm-bridge attach --mission {short}" in out
        assert mission_id not in out.split(" · controller log:")[0]  # only the log path carries the full ID
        assert f"llm-bridge status --mission {short} shows how to approve" in out
        assert "controller log:" in out
    finally:
        _call(env, "stop", "--mission", mission_id, "--force")
        _wait(lambda: _released(state, mission_id))
        if socket.exists():
            subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
    with BridgeStore(state / "bridge.sqlite3") as store:
        assert store.get_mission(mission_id)["status"] == "STOPPED"


def test_headless_start_points_at_status_instead_of_attach(tmp_path, env) -> None:
    mission_id, state = _mission(env, tmp_path, "--headless")
    short = mission_id[:8]
    try:
        out = _call(env, "start", "--mission", mission_id, "--headless")
        assert f"mission {short} running without a terminal station · inspect with: llm-bridge status --mission {short}" in out
        assert "attach" not in out
    finally:
        _call(env, "stop", "--mission", mission_id, "--force")
        _wait(lambda: _released(state, mission_id))


def test_follow_up_task_reopens_a_completed_mission_and_start_runs_it(tmp_path, env) -> None:
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}})
    provider = tmp_path / "provider.py"
    provider.write_text("from pathlib import Path\nimport sys\nPath('runs.txt').open('a').write(Path(sys.argv[1]).read_text().split('Task ')[1].split(' ')[0] + '\\n')\nprint('done', flush=True)\n")
    verifier = tmp_path / "verify.py"
    verifier.write_text("print('{\"passed\": true}')\n")
    created = json.loads(_call(env, "mission", "create", "--prompt", "First job", "--cwd", tmp_path, "--headless", "--providers", "codex",
                               "--command-template", shlex.join([sys.executable, str(provider), "{prompt_file}"]),
                               "--verifier-arg", sys.executable, "--verifier-arg", verifier, "--json"))
    mid = created["mission"]["id"]

    def status() -> str:
        with BridgeStore(state / "bridge.sqlite3") as store:
            return store.get_mission(mid)["status"]

    try:
        _call(env, "start", "--mission", mid, "--headless")
        _wait(lambda: status() == "COMPLETED")
        _wait(lambda: _released(state, mid))
        out = _call(env, "task", "add", "--mission", mid, "--prompt", "Follow-up job")
        assert "Mission reopened · start it with: llm-bridge start" in out
        assert status() == "STOPPED"
        assert "STOPPED" in _call(env, "status", "--mission", mid)
        _call(env, "start", "--mission", mid, "--headless")
        _wait(lambda: status() == "COMPLETED")
        _wait(lambda: _released(state, mid))
        assert (tmp_path / "runs.txt").read_text().split() == ["1", "2"]  # the provider was told each task's mission number
        assert "mission-reopened" in _call(env, "events", "--mission", mid)
    finally:
        if status() not in {"STOPPED", "COMPLETED", "FAILED", "CANCELLED"}:
            _call(env, "stop", "--mission", mid, "--force")
        _wait(lambda: _released(state, mid))
