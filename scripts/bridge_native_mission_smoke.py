#!/usr/bin/env python3
"""Opt-in two-prompt Codex native reply/resume through the full Bridge controller.

Run before committing major terminal/session changes. This uses real capacity,
real provider argv, private tmux and a disposable repository. No raw provider
output or credentials are saved. CI tests the oracle; live calls are ad-hoc only.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from llm_tools.bridge_store import BridgeStore, MissionLock  # noqa: E402
from llm_tools.bridge_runtime import mission_socket_path  # noqa: E402
from llm_tools.terminal import TmuxTerminalBackend  # noqa: E402


def answer_seen(screen: str, expected: str) -> bool:
    return any(re.fullmatch(r'\s*[•●]\s*'+re.escape(expected)+r'\s*', line)
               for line in screen.splitlines())


@contextmanager
def probe_workspace(result: dict):
    """Never discard the only ownership metadata for an uncertain live probe."""
    root = Path(tempfile.mkdtemp(prefix='lb-native-mission-'))
    try:
        yield root
    finally:
        if result.get('cleanup') == 'PASS':
            shutil.rmtree(root)
        else:
            result['retained_state'] = str(root)
            (root/'probe-result.json').write_text(json.dumps(result,indent=2)+'\n')


def probe(seconds: int) -> dict:
    backend = TmuxTerminalBackend()
    result = {'provider':'codex','initial_prompt_budget':2,'native_reply':'NOT VERIFIED',
              'native_resume':'NOT VERIFIED','provider_mouse':'NOT VERIFIED', 'cleanup':'NOT VERIFIED'}
    version = subprocess.run(['codex','--version'], text=True, capture_output=True, timeout=10)
    result['version'] = version.stdout.strip()
    with probe_workspace(result) as root:
        cwd, state = root/'work', root/'state'
        cwd.mkdir()
        subprocess.run(['git','init','-q',str(cwd)], check=True)
        env = {**os.environ,'LLM_BRIDGE_STATE_DIR':str(state),'TERM':'xterm-256color'}
        env['PYTHONPATH'] = os.pathsep.join(filter(None,(str(ROOT), env.get('PYTHONPATH',''))))
        env.pop('LLM_SCHEDULER_USAGE_JSON', None)
        def cli(*args):
            response = subprocess.run([sys.executable,'-m','llm_tools.bridge',*map(str,args)],
                env=env, text=True, capture_output=True, timeout=20)
            if response.returncode:
                raise RuntimeError('Bridge command failed: '+str(args[0]))
            return response.stdout
        mid, handle = None, None
        def task_state(task):
            with BridgeStore(state/'bridge.sqlite3') as store: return store.get_task(task)['status']
        def wait(predicate, timeout):
            deadline = time.monotonic()+timeout
            while time.monotonic()<deadline:
                if predicate(): return True
                time.sleep(.1)
            return bool(predicate())
        def released():
            lock = MissionLock(state/'locks',mid)
            try: lock.acquire()
            except BlockingIOError: return False
            lock.release(); return True
        def get_handle():
            return backend.open_existing(socket_path=mission_socket_path(mid,env=env),session_name='mission-'+mid)
        def reply(expected, timeout):
            trusted = False
            deadline = time.monotonic()+timeout
            while time.monotonic()<deadline:
                screen = backend.capture(handle,pane='provider')
                if 'Trust this folder?' in screen and not trusted:
                    backend.send_keys(handle,pane='provider',keys='Enter');trusted=True
                if answer_seen(screen,expected): return True
                time.sleep(.1)
            return False
        def exit_provider(task):
            backend.send_keys(handle,pane='provider',keys='C-d')
            if not wait(lambda: task_state(task)=='VERIFYING',3):
                backend.send_keys(handle,pane='provider',keys='C-d')
            if not wait(lambda: task_state(task)=='VERIFYING',10):
                raise RuntimeError('Native provider did not exit cleanly to VERIFYING')
        try:
            created = json.loads(cli('mission','create','--prompt',
                'Reply only with uppercase bridge, underscore, uppercase native, underscore, uppercase ok. Do not use tools or inspect files.',
                '--cwd',cwd,'--providers','codex','--native','--json'))
            mid, first = created['mission']['id'], created['task']['id']
            cli('start','--mission',mid)
            handle = get_handle()
            if not reply('BRIDGE_NATIVE_OK',seconds): return result
            result['native_reply'] = 'PASS'
            with BridgeStore(state/'bridge.sqlite3') as store:
                identity = store.get_mission(mid)['config'].get('native_sessions',{}).get('codex',{})
            session_id = identity.get('session_id')
            if not session_id: return result
            result['identity_source'] = identity.get('source')
            second = json.loads(cli('task','add','--mission',mid,'--prompt',
                'Recall the marker from your first reply. Return only that marker followed by underscore and uppercase resumed. Do not use tools.',
                '--json'))['id']
            exit_provider(first)
            cli('stop','--mission',mid)
            if not wait(released,10): raise RuntimeError('Controller lock retained after stop')
            cli('task','verify','--mission',mid,'--task',first,'--evidence','Native response matched the script oracle')
            cli('resume','--mission',mid)
            handle = get_handle()
            if not reply('BRIDGE_NATIVE_OK_RESUMED',seconds): return result
            with BridgeStore(state/'bridge.sqlite3') as store:
                operations = store.db.execute('SELECT native_session_id,restoration_state FROM operations WHERE mission_id=? ORDER BY rowid',(mid,)).fetchall()
            if len(operations)==2 and operations[0][0]==operations[1][0]==session_id and operations[1][1]=='RESTORED_NATIVE':
                result['native_resume'] = 'PASS: same owned UUID, new controller, new response recalls history'
            exit_provider(second)
        except Exception as exc:
            result['error'] = type(exc).__name__
        finally:
            if mid:
                try:
                    cli('stop','--mission',mid,'--force')
                    result['cleanup'] = 'PASS' if wait(released,10) else 'FAILED: controller lock retained'
                except Exception as exc:
                    result['cleanup'] = 'FAILED: '+type(exc).__name__
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-call-budget',required=True,type=int,choices=[2])
    parser.add_argument('--seconds',type=int,choices=range(10,61),default=35)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args(argv)
    result = probe(args.seconds)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return int(not all(str(result[key]).startswith('PASS') for key in ('native_reply','native_resume','cleanup')))


if __name__ == '__main__':
    raise SystemExit(main())
