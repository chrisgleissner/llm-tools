"""Exact native IDs across real controller subprocesses and private tmux PTYs."""
import json
import os
import subprocess
import sys
import time

import pytest

from llm_tools.bridge_store import BridgeStore, MissionLock


def cli(env, *args):
    result = subprocess.run([sys.executable, "-m", "llm_tools.bridge", *map(str, args)],
                            env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    assert predicate(), "native controller did not reach expected state"


@pytest.mark.parametrize("provider", ["claude", "copilot", "codex"])
def test_native_session_id_survives_full_controller_shutdown(tmp_path, env, fake_bin, provider):
    state = tmp_path / "state"
    env["LLM_BRIDGE_STATE_DIR"] = str(state)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude-config")
    env["COPILOT_CONFIG_DIR"] = str(tmp_path / "copilot-config")
    env["CODEX_HOME"] = str(tmp_path / "codex-config")
    ready = {"available": True, "five_hour": {"remaining": 80}, "week": {"remaining": 80}, "monthly": {"remaining": 80}}
    env["LLM_SCHEDULER_USAGE_JSON"] = json.dumps({provider: ready})
    binary = fake_bin / provider
    binary.write_text("#!" + sys.executable + "\n" + """import json, os, sys, time
from pathlib import Path
if '--help' in sys.argv:
    print('Usage: --session-id UUID --resume ID --no-daemon\\n  resume  Resume session'); raise SystemExit(0)
provider = Path(sys.argv[0]).name
mode = 'resume' if '--resume' in sys.argv or 'resume' in sys.argv else 'new'
flag = 'resume' if provider == 'codex' else '--resume' if mode == 'resume' else '--session-id'
if provider == 'codex' and mode == 'new':
    import uuid
    session_id = str(uuid.uuid4())
else:
    session_id = sys.argv[sys.argv.index(flag)+1]
cwd = str(Path.cwd().resolve())
if provider == 'codex':
    assert '--no-daemon' in sys.argv
    path = Path(os.environ['CODEX_HOME'])/'sessions'/('rollout-'+session_id+'.jsonl')
    record = {'type':'session_meta','payload':{'id':session_id,'cwd':cwd}}
elif provider == 'claude':
    path = Path(os.environ['CLAUDE_CONFIG_DIR'])/'projects'/cwd.replace('/','-')/(session_id+'.jsonl')
    record = {'type':'system','subtype':'init','session_id':session_id,'cwd':cwd}
else:
    path = Path(os.environ['COPILOT_CONFIG_DIR'])/'session-state'/session_id/'events.jsonl'
    record = {'type':'session.start','data':{'sessionId':session_id,'context':{'cwd':cwd}}}
if mode == 'resume': assert path.exists()
path.parent.mkdir(parents=True, exist_ok=True)
with path.open('a') as output: output.write(json.dumps(record)+'\\n')
with Path('native-calls.jsonl').open('a') as output: output.write(json.dumps({'mode':mode,'id':session_id,'pid':os.getpid(),'args':sys.argv[1:]})+'\\n')
held = path.open('a')
print('native fake completed', flush=True)
time.sleep(1.5 if provider == 'codex' else .1)
""")
    binary.chmod(0o700)
    created = json.loads(cli(env, "mission", "create", "--prompt", "First native task",
                            "--cwd", tmp_path, "--native", "--providers", provider, "--json"))
    mid, first = created["mission"]["id"], created["task"]["id"]
    database = state / "bridge.sqlite3"
    ready_file = state / "runtime" / f"mission-{mid}" / "controller-ready.json"
    def task_state(task):
        with BridgeStore(database) as store:
            return store.get_task(task)["status"]
    def released():
        lock = MissionLock(state / "locks", mid)
        try:
            lock.acquire()
        except BlockingIOError:
            return False
        lock.release()
        return True
    try:
        cli(env, "start", "--mission", mid)
        wait(lambda: task_state(first) == "VERIFYING")
        initial_ready = json.loads(ready_file.read_text())
        with BridgeStore(database) as store:
            saved_id = store.get_mission(mid)["config"]["native_sessions"][provider]["session_id"]
        cli(env, "stop", "--mission", mid)
        wait(released)
        second = json.loads(cli(env, "task", "add", "--mission", mid, "--prompt", "Second native task", "--json"))["id"]
        cli(env, "task", "verify", "--mission", mid, "--task", first, "--approve")
        cli(env, "resume", "--mission", mid)
        wait(lambda: task_state(second) == "VERIFYING")
        restarted_ready = json.loads(ready_file.read_text())
        assert restarted_ready["startup_token"] != initial_ready["startup_token"]
        with BridgeStore(database) as store:
            operations = store.db.execute("SELECT native_session_id,restoration_state FROM operations WHERE mission_id=? ORDER BY rowid", (mid,)).fetchall()
        assert [(row[0], row[1]) for row in operations] == [(saved_id, "NATIVE_CREATED"), (saved_id, "RESTORED_NATIVE")]
        calls = [json.loads(line) for line in (tmp_path / "native-calls.jsonl").read_text().splitlines()]
        assert len(calls) == 2 and calls[0]["pid"] != calls[1]["pid"]
        assert calls[1]["mode"] == "resume" and calls[1]["id"] == calls[0]["id"] == saved_id
        assert "--last" not in calls[1]["args"]
    finally:
        cli(env, "stop", "--mission", mid, "--force")
        wait(released)
