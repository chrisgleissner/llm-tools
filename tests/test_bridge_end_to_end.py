"""Real detached controller acceptance without paid services."""
import json
import shlex
import subprocess
import sys
import time

from llm_tools.bridge_store import BridgeStore, MissionLock


def call(env, *args):
    result = subprocess.run([sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
                            env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    assert predicate(), "detached mission did not reach expected state"


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
