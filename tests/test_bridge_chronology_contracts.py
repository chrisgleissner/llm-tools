"""Opaque UUIDs must never determine chronological operator or handoff history."""
from types import SimpleNamespace
from llm_tools import bridge_store
from llm_tools.bridge_store import BridgeStore
from llm_tools.bridge_runtime import BridgeMissionHooks


def test_same_second_queued_instructions_keep_submission_order(tmp_path,monkeypatch):
    with BridgeStore(tmp_path/'state.sqlite3') as store:
        mid = store.create_mission('FIFO','objective',cwd=tmp_path)['id']
        monkeypatch.setattr(bridge_store,'utc_now',lambda:'2026-10-27T12:00:00+00:00')
        store.enqueue_command(mid,{'action':'pause'},command_id='z-first')
        store.enqueue_command(mid,{'action':'resume'},command_id='a-second')
        assert [command['payload']['action'] for command in store.pending_commands(mid)] == ['pause','resume']


def test_handoff_lists_newest_attempt_first_when_timestamps_match(tmp_path,monkeypatch):
    with BridgeStore(tmp_path/'state.sqlite3') as store:
        mid = store.create_mission('handoff','objective',cwd=tmp_path)['id']
        task = store.add_task(mid,'continue safely')
        store.set_mission_status(mid,'RUNNING')
        ids = iter(['f'*32,'0'*32])
        monkeypatch.setattr(bridge_store.uuid,'uuid4',lambda:SimpleNamespace(hex=next(ids)))
        monkeypatch.setattr(bridge_store,'utc_now',lambda:'2026-10-27T12:00:00+00:00')
        attempts = []
        for index,station in enumerate(['claude','codex'],1):
            operation = store.begin_operation(task,iteration=index,station=station)
            attempts.append(operation)
            store.finish_operation(operation,exit_code=75)
            store.continue_task_after_provider_failure(operation,max_failures=3)
        hooks = BridgeMissionHooks(mid,handle=None,headless=True,controller_logs=tmp_path/'controller.log')
        hooks.bind(store)
        context = hooks.before_iteration()
        payload = context['prompt'].split('Durable checkpoint (latest bounded history):\n',1)[1].split('\n\nContinue',1)[0]
        assert [line.split()[2].rstrip(':') for line in payload.splitlines() if line.startswith('- operation ')] == list(reversed(attempts))
