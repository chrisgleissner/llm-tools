"""Real detached controller acceptance without paid services."""
import json
import shlex
import subprocess
import sys
import time

from llm_tools.bridge_store import BridgeStore, MissionLock
from tests.conftest import scaled, wait_for as _shared_wait_for


def call(env, *args):
    result = subprocess.run([sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
                            env=env, text=True, capture_output=True, timeout=scaled(15))
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def wait_for(predicate, timeout=15):
    return _shared_wait_for(predicate, timeout, .05, message="detached mission did not reach expected state")


def controller_released(state, mission):
    lock = MissionLock(state / "locks", mission)
    try:
        lock.acquire()
    except BlockingIOError:
        return False
    lock.release()
    return True


def test_provider_abort_hands_same_task_to_next_provider(tmp_path, env):
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    ready = {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}}
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": ready, "claude": ready})
    fake = tmp_path / "handoff.py"
    fake.write_text("""from pathlib import Path
import sys
provider = sys.argv[1]
with Path('providers').open('a') as out: out.write(provider+'\\n')
if provider == 'codex':
    Path('checkpoint.txt').write_text('prior provider work')
    print('route unavailable', flush=True)
    raise SystemExit(75)
assert Path('checkpoint.txt').read_text() == 'prior provider work'
assert 'Durable checkpoint' in Path(sys.argv[2]).read_text()
Path('result.txt').write_text('completed by second provider')
print('work completed', flush=True)
""")
    verifier = tmp_path / "verify.py"
    verifier.write_text("from pathlib import Path\nimport json\nassert Path('result.txt').exists()\nprint(json.dumps({'passed': True,'artifacts':['result.txt']}))\n")
    created = json.loads(call(env, "mission", "create", "--prompt", "Continue through a provider handoff",
        "--cwd", tmp_path, "--headless", "--providers", "codex,claude", "--command-template",
        shlex.join([sys.executable, str(fake), "{provider}", "{prompt_file}"]),
        "--verifier-arg", sys.executable, "--verifier-arg", verifier, "--json"))
    mid, task = created["mission"]["id"], created["task"]["id"]
    path = state / "bridge.sqlite3"
    def status():
        with BridgeStore(path) as store:
            return store.get_mission(mid)["status"]
    try:
        call(env, "start", "--mission", mid, "--headless")
        wait_for(lambda: status() == "COMPLETED", timeout=8)
        with BridgeStore(path) as store:
            operations = store.db.execute("SELECT station,exit_code FROM operations WHERE task_id=? ORDER BY rowid", (task,)).fetchall()
            assert [(row[0], row[1]) for row in operations] == [("codex", 75), ("claude", 0)]
            assert store.get_task(task)["revision"] == 1
            assert store.get_task(task)["evidence"]
        assert (tmp_path / "providers").read_text().splitlines() == ["codex", "claude"]
    finally:
        if status() not in {"STOPPED", "COMPLETED", "FAILED", "CANCELLED"}:
            call(env, "stop", "--mission", mid)
        wait_for(lambda: controller_released(state, mid))


def test_detached_submission_amendment_and_verification(tmp_path, env):
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": {"available": True,
        "five_hour": {"remaining": 80}, "week": {"remaining": 80}}})
    release = tmp_path / "release"
    fake = tmp_path / "provider.py"
    fake.write_text("""from pathlib import Path
import json, sys, time
prompt = Path(sys.argv[1]).read_text()
with Path('calls.jsonl').open('a') as out: out.write(json.dumps(prompt)+'\\n')
print('FAKE PROVIDER ACTIVE', flush=True)
deadline = time.monotonic()+20
while not Path('release').exists() and time.monotonic()<deadline: time.sleep(.05)
if not Path('release').exists(): raise SystemExit(2)
Path('artifact.txt').write_text('verified artifact')
print('FAKE PROVIDER FINISHED', flush=True)
""")
    verifier = tmp_path / "verifier.py"
    verifier.write_text("from pathlib import Path\nimport json\nassert Path('artifact.txt').read_text() == 'verified artifact'\nprint(json.dumps({'passed': True, 'artifacts': ['artifact.txt']}))\n")
    template = shlex.join([sys.executable, str(fake), "{prompt_file}"])
    created = json.loads(call(env, "mission", "create", "--prompt", "Original task",
        "--cwd", tmp_path, "--headless", "--providers", "codex", "--command-template", template,
        "--verifier-arg", sys.executable, "--verifier-arg", verifier, "--json"))
    mid, first = created["mission"]["id"], created["task"]["id"]
    path = state / "bridge.sqlite3"
    def mission_state():
        with BridgeStore(path) as store:
            return store.get_mission(mid)["status"]
    try:
        call(env, "start", "--mission", mid, "--headless")
        wait_for(lambda: (tmp_path / "calls.jsonl").exists())
        with BridgeStore(path) as store:
            assert store.get_task(first)["status"] == "RUNNING"
        added = json.loads(call(env, "task", "add", "--mission", mid, "--prompt", "Dependent task",
                               "--depends-on", first, "--json"))
        call(env, "task", "amend", "--mission", mid, "--task", first, "--prompt", "Amended task")
        release.touch()
        wait_for(lambda: mission_state() == "COMPLETED")
        with BridgeStore(path) as store:
            assert store.get_task(first)["revision"] == 2
            assert store.get_task(first)["status"] == "COMPLETED"
            assert store.get_task(added["id"])["status"] == "COMPLETED"
            assert store.get_task(first)["evidence"]
        prompts = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        assert len(prompts) == 3
        assert "Amended task" in prompts[1] and "Dependent task" in prompts[2]
    finally:
        release.touch()
        if mission_state() not in {"COMPLETED", "STOPPED", "CANCELLED", "FAILED"}:
            call(env, "stop", "--mission", mid)
        def controller_released():
            lock = MissionLock(state / "locks", mid)
            try:
                lock.acquire()
            except BlockingIOError:
                return False
            lock.release()
            return True
        wait_for(controller_released)


def test_pause_graceful_shutdown_and_restart_do_not_replay_completed_provider_work(tmp_path, env):
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({"codex": {"available": True,
        "five_hour": {"remaining": 80}, "week": {"remaining": 80}}})
    release = tmp_path / "release"
    fake = tmp_path / "provider.py"
    fake.write_text("""from pathlib import Path
import time
with Path('calls').open('a') as out: out.write('run\\n')
print('active', flush=True)
deadline = time.monotonic()+20
while not Path('release').exists() and time.monotonic()<deadline: time.sleep(.05)
if not Path('release').exists(): raise SystemExit(2)
print('finished', flush=True)
""")
    created = json.loads(call(env, "mission", "create", "--prompt", "Work requiring approval",
        "--cwd", tmp_path, "--headless", "--providers", "codex", "--command-template",
        shlex.join([sys.executable, str(fake)]), "--json"))
    mid, task = created["mission"]["id"], created["task"]["id"]
    path = state / "bridge.sqlite3"
    def states():
        with BridgeStore(path) as store:
            return store.get_mission(mid)["status"], store.get_task(task)["status"]
    try:
        call(env, "start", "--mission", mid, "--headless")
        wait_for(lambda: (tmp_path / "calls").exists())
        call(env, "pause", "--mission", mid)
        release.touch()
        wait_for(lambda: states()[1] != "RUNNING")
        assert states() == ("PAUSED", "VERIFYING")
        call(env, "stop", "--mission", mid)
        wait_for(lambda: states()[0] == "STOPPED")
        wait_for(lambda: controller_released(state, mid))
        assert not (state / "runtime" / f"mission-{mid}" / "tmux.sock").exists()
        call(env, "resume", "--mission", mid)
        wait_for(lambda: states()[0] == "RUNNING")
        assert states()[1] == "VERIFYING"
        call(env, "task", "verify", "--mission", mid, "--task", task, "--approve")
        wait_for(lambda: states()[0] == "COMPLETED")
        assert (tmp_path / "calls").read_text().splitlines() == ["run"]
    finally:
        release.touch()
        if states()[0] not in {"COMPLETED", "STOPPED", "CANCELLED", "FAILED"}:
            call(env, "stop", "--mission", mid)
        wait_for(lambda: controller_released(state, mid))
