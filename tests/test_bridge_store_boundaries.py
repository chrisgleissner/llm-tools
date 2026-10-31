"""Durability boundaries: rejected requests never leave partial durable effects."""
import subprocess
import pytest
from llm_tools.bridge_store import BridgeStore, MissionLock, TransitionError, repository_identity


@pytest.fixture
def store_mission(tmp_path):
    with BridgeStore(tmp_path / 'private' / 'state.sqlite3') as store:
        mission = store.create_mission('boundaries', 'objective', cwd=tmp_path)
        yield store, mission['id']


def test_missing_objects_and_invalid_requests_do_not_write_events(store_mission):
    store, mid = store_mission
    before = store.events(mid)
    for request in [lambda: store.get_task(999), lambda: store.get_mission('missing'),
                    lambda: store.get_operation('missing'), lambda: store.get_command('missing'),
                    lambda: store.get_managed_process('missing'),
                    lambda: store.finish_command('missing', status='APPLIED'),
                    lambda: store.finish_operation('missing', exit_code=0),
                    lambda: store.continue_task_after_provider_failure('missing', max_failures=1),
                    lambda: store.record_operation_process('missing', process_pid=2, process_start='token'),
                    lambda: store.record_operation_session('missing', restoration_state='UNAVAILABLE', provider='codex')]:
        with pytest.raises(KeyError): request()
    for request in [lambda: store.add_task(mid, ''), lambda: store.set_mission_status(mid, 'UNKNOWN'),
                    lambda: store.set_task_status(999, 'UNKNOWN'),
                    lambda: store.finish_command('missing', status='UNKNOWN'),
                    lambda: store.enqueue_command(mid, {}, command_id='../escape'),
                    lambda: store.record_runtime_event(mid, '', {}),
                    lambda: store.record_runtime_event(mid, 'large', {'data': 'x'*16384}),
                    lambda: store.set_mission_config_value(mid, 'cwd', '/', event_kind='bad'),
                    lambda: store.set_mission_config_values(mid, {}, event_kind='bad'),
                    lambda: store.set_mission_config_values(mid, {'objective':'changed'}, event_kind='bad'),
                    lambda: store.continue_task_after_provider_failure('missing', max_failures=0),
                    lambda: store.record_operation_process('missing', process_pid=0, process_start=''),
                    lambda: store.record_operation_session('missing', restoration_state='UNKNOWN', provider='codex'),
                    lambda: store.record_operation_session('missing', restoration_state='RESTORED_NATIVE', provider='codex'),
                    lambda: store.mark_managed_process_running('missing', process_pid=1, process_start=''),
                    lambda: store.finish_managed_process('missing', exit_code=0, status='UNKNOWN'),
                    lambda: store.prepare_managed_process(mid, 999, kind='provider')]:
        with pytest.raises(ValueError): request()
    assert store.events(mid) == before


def test_command_reuse_is_idempotent_but_rejects_different_request(store_mission):
    store, mid = store_mission
    request = {'action':'pause'}
    store.enqueue_command(mid, request, command_id='retry-key')
    store.enqueue_command(mid, request, command_id='retry-key')
    assert len(store.pending_commands(mid)) == 1
    with pytest.raises(TransitionError):
        store.enqueue_command(mid, {'action':'cancel'}, command_id='retry-key')
    first = store.finish_command('retry-key', status='REJECTED', result={'reason':'test'})
    assert store.finish_command('retry-key', status='APPLIED') == first
    assert store.pending_commands(mid) == []


def test_terminal_tasks_cannot_be_edited_reordered_or_receive_dependencies(store_mission):
    store, mid = store_mission
    dependency = store.add_task(mid, 'prerequisite')
    terminal = store.add_task(mid, 'cancelled')
    store.set_task_status(terminal, 'CANCELLED')
    before = store.get_task(terminal)
    for request in [lambda: store.amend_task(terminal, prompt='changed'),
                    lambda: store.reorder_task(terminal, 10),
                    lambda: store.set_task_priority(terminal, 5),
                    lambda: store.add_dependency(mid, terminal, dependency)]:
        with pytest.raises(TransitionError): request()
    assert store.get_task(terminal) == before


def test_managed_verifier_identity_cannot_be_rewritten_after_start_or_exit(store_mission):
    store, mid = store_mission
    task = store.add_task(mid, 'verify')
    with pytest.raises(TransitionError): store.prepare_managed_process(mid, task, kind='verifier')
    store.set_mission_status(mid, 'RUNNING')
    operation = store.begin_operation(task, iteration=1, station='codex')
    with pytest.raises(TransitionError):
        store.record_operation_process(operation, process_pid=12, process_start='token')
    store.finish_operation(operation, exit_code=0)
    process = store.prepare_managed_process(mid, task, kind='verifier')
    assert len(store.unresolved_managed_processes(mid)) == 1
    store.mark_managed_process_running(process, process_pid=12, process_start='boot:token')
    with pytest.raises(TransitionError):
        store.mark_managed_process_running(process, process_pid=13, process_start='different')
    result = store.finish_managed_process(process, exit_code=0)
    assert store.finish_managed_process(process, exit_code=0) == result
    with pytest.raises(TransitionError): store.finish_managed_process(process, exit_code=1)
    assert store.unresolved_managed_processes(mid) == []


def test_failures_with_amendments_are_not_automatically_replayed(store_mission):
    store, mid = store_mission
    task = store.add_task(mid, 'work')
    store.set_mission_status(mid, 'RUNNING')
    operation = store.begin_operation(task, iteration=1, station='codex')
    assert store.continue_task_after_provider_failure(operation, max_failures=1) is None
    store.amend_task(task, prompt='new instruction')
    store.finish_operation(operation, exit_code=2)
    assert store.continue_task_after_provider_failure(operation, max_failures=1) is None
    assert store.get_task(task)['status'] == 'FAILED'
    assert store.get_task(task)['pending']['prompt'] == 'new instruction'
    with pytest.raises(TransitionError): store.finish_operation(operation, exit_code=0)


def test_git_probe_failure_and_invalid_lock_degrade_safely(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs): raise subprocess.TimeoutExpired('git', 2)
    monkeypatch.setattr(subprocess, 'run', unavailable)
    assert repository_identity(tmp_path) == str(tmp_path)
    with pytest.raises(ValueError): MissionLock(tmp_path, '../escape')
    directory = tmp_path / 'public'
    directory.mkdir(mode=0o755)
    with pytest.raises(PermissionError): MissionLock(directory, 'mission').acquire()
    with MissionLock(tmp_path / 'locks', 'safe') as lock:
        lock.acquire()
    lock.release()


def test_event_pages_have_stable_cursors_and_zero_does_not_read_every_event(store_mission):
    store, mid = store_mission
    for index in range(250): store.record_runtime_event(mid, 'sample', {'index':index})
    first = store.events(mid, limit=100)
    second = store.events(mid, after=first[-1]['id'], limit=100)
    assert len(first) == len(second) == 100
    assert first[-1]['id'] < second[0]['id']
    assert store.events(mid, limit=0) == []
    with pytest.raises(ValueError): store.events(mid, limit=-1)
    with pytest.raises(ValueError): store.events(mid, limit=1001)
