"""Actual Bridge command pane under sustained fake provider output."""
import json
import shlex
import shutil
import subprocess
import sys
import time

from llm_tools.bridge_runtime import mission_socket_path
from llm_tools.bridge_store import BridgeStore, MissionLock
from llm_tools.terminal import TmuxTerminalBackend
from tests.conftest import scaled, wait_for as _shared_wait_for


def call(env, *args):
    completed = subprocess.run([sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
                               env=env, text=True, capture_output=True, timeout=scaled(15))
    assert completed.returncode == 0, completed.stderr + completed.stdout
    return completed.stdout


def wait_for(predicate, timeout=15):
    return _shared_wait_for(predicate, timeout, .01, message="streaming mission did not reach expected state")


def controller_released(state, mission):
    lock = MissionLock(state / "locks", mission)
    try:
        lock.acquire()
    except BlockingIOError:
        return False
    lock.release()
    return True


def test_console_commands_commit_while_provider_streams(tmp_path, env):
    assert shutil.which("tmux"), "CI must install tmux"
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": {"available": True,
        "five_hour": {"remaining": 80}, "week": {"remaining": 80}}})
    fake = tmp_path / "stream.py"
    fake.write_text("""from pathlib import Path
import sys, time, json
Path('streaming').touch()
block = 'X'*16383+'\\n'
count = 0
started = time.monotonic()
while not Path('release').exists() and time.monotonic()-started < 20:
    sys.stdout.write(block); sys.stdout.flush(); count += len(block); time.sleep(1/64)
Path('stream-count.json').write_text(json.dumps({'bytes':count,'seconds':time.monotonic()-started}))
""")
    created = json.loads(call(env, "mission", "create", "--prompt", "Streaming fake provider",
        "--cwd", tmp_path, "--providers", "codex", "--command-template",
        shlex.join([sys.executable, str(fake)]), "--json"))
    mid, task = created["mission"]["id"], created["task"]["id"]
    backend = TmuxTerminalBackend()
    handle = None
    latencies = []
    try:
        with BridgeStore(state / "bridge.sqlite3") as store:
            # One outer transaction: add_task nests as savepoints instead of committing 10,000 times.
            with store._transaction():
                for index in range(10000):
                    store.add_task(mid, f"queued stress order {index}")
        call(env, "start", "--mission", mid)
        wait_for(lambda: (tmp_path / "streaming").exists())
        handle = backend.open_existing(mission_socket_path(mid, env=env), f"mission-{mid}")
        wait_for(lambda: "LLM BRIDGE" in backend.capture(handle, pane="bridge"))
        loaded_at = time.monotonic()
        with BridgeStore(state / "bridge.sqlite3") as store:
            for index in range(8):
                value = 100 + index
                started = time.perf_counter()
                backend.send_keys(handle, pane="bridge", keys=f"priority {task} {value}")
                backend.send_keys(handle, pane="bridge", keys="Enter")
                wait_for(lambda: store.get_task(task)["priority"] == value, timeout=2)
                latencies.append(time.perf_counter() - started)
                # The console rejects a command typed while the previous one is pending ("A command is still
                # running"), and it clears that state only when it applies the result, which it prints. The
                # database row changes earlier, on the worker thread, so wait for the printed result.
                wait_for(lambda: f"Applied: priority {task} {value}" in backend.capture(handle, pane="bridge"))
        assert not (tmp_path / "stream-count.json").exists(), "load must still be active during input measurements"
        while time.monotonic() - loaded_at < 3:
            time.sleep(.02)
        call(env, "pause", "--mission", mid)
        (tmp_path / "release").touch()
        wait_for(lambda: (tmp_path / "stream-count.json").exists())
        load = json.loads((tmp_path / "stream-count.json").read_text())
        metrics = {"input_commit_latencies_seconds": latencies, "output_bytes": load["bytes"],
                   "output_seconds": load["seconds"], "max_input_commit_seconds": max(latencies)}
        (tmp_path / "stress-results.json").write_text(json.dumps(metrics, indent=2))
        assert load["bytes"] >= 128 * 1024
        # A CI regression guard; local measurements also report the 250ms target.
        assert max(latencies) < scaled(.75), metrics
    finally:
        (tmp_path / "release").touch()
        call(env, "stop", "--mission", mid)
        wait_for(lambda: controller_released(state, mid))
        if handle:
            backend.stop(handle)
