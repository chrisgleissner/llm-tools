"""Authoritative source acquisition must never turn failures into amendments."""
import json
import time
from pathlib import Path

import pytest

from llm_tools import bridge, bridge_tui as tui


def settle(state, predicate):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        state.refresh()
        if predicate(): return
        time.sleep(.005)
    assert predicate()


@pytest.mark.parametrize('reply',[None,'not-json','{}',json.dumps({'prompt':123})])
def test_failed_full_read_never_opens_an_edit_with_error_as_source(reply):
    def dispatch(*_):
        if reply is None: raise OSError('source unavailable')
        return reply
    state=tui._ConsoleState('mission',dispatch,lambda *_: {},full=True)
    state.request_task_document('1',for_edit=True)
    settle(state,lambda:not state.document_pending)
    assert not state.edit_document_requested
    assert state.edit_initial_text=='' and state.document_viewer is None
    assert any('unavailable' in line.lower() or 'could not read' in line.lower() for line in state.output)


def test_full_objective_is_authoritative_and_failure_does_not_create_source(tmp_path,monkeypatch):
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    source='# Full objective\r\n'+'中  source\n'*20000+'TAIL\n'
    with bridge.open_store() as store:
        mission=store.create_mission('Read',source,cwd=tmp_path)
    state=tui._ConsoleState(mission['id'],lambda *_:'',lambda *_:{},full=True)
    state.snapshot={'mission':{'objective':'clipped preview'}}
    state.handle_key('o')
    settle(state,lambda:state.document_viewer is not None)
    assert state.document_viewer.source==source
    state.handle_key('e')
    assert not state.edit_document_requested
    assert state.document_viewer.source==source
    state.handle_key('q')
    state.mission_id='missing'
    state.handle_key('o')
    settle(state,lambda:not state.document_pending)
    assert state.document_viewer is None and not state.edit_document_requested
    assert 'Could not read full objective' in state.output[-1]


@pytest.mark.parametrize('helper',['wl-copy','xclip','xsel','pbcopy',None])
def test_clipboard_helpers_are_explicit_local_argv_and_missing_is_supported(monkeypatch,helper):
    monkeypatch.setattr(tui.shutil,'which',lambda name: '/bin/'+name if name==helper else None)
    argv=tui._clipboard_argv()
    assert argv is None if helper is None else argv[0]==helper
    if helper=='xclip': assert argv==['xclip','-selection','clipboard']
    if helper=='xsel': assert argv==['xsel','--clipboard','--input']


def test_export_permission_failure_keeps_path_source_and_read_only_context(monkeypatch,tmp_path):
    from llm_tools.bridge_documents import DocumentViewer
    source='complete  source\r\nTAIL\n'
    state=tui._ConsoleState('mission',lambda *_:None,lambda *_:{},full=True)
    state.snapshot={'mission':{'status':'COMPLETED'}}
    state.document_task_id='1'
    state.document_viewer=DocumentViewer('Task 1 r2',source)
    target=tmp_path/'forbidden.md'
    real_open=tui.os.open
    def deny(path,*args,**kwargs):
        if Path(path)==target: raise PermissionError('permission denied')
        return real_open(path,*args,**kwargs)
    monkeypatch.setattr(tui.os,'open',deny)
    state.handle_key('s')
    for char in str(target): state.handle_key(char)
    state.handle_key('\n')
    assert state.document_export_editing and state.document_entry==str(target)
    assert 'permission denied' in state.document_notice
    assert state.document_viewer.source==source
    state.handle_key('\x1b')
    state.handle_key('e')
    assert not state.edit_document_requested
    assert 'terminal mission' in state.document_notice
    assert state.document_viewer.source==source


def test_failed_task_file_creation_retains_complete_checkpoint_and_reports_it(monkeypatch,tmp_path):
    monkeypatch.setenv('LLM_BRIDGE_STATE_DIR',str(tmp_path/'state'))
    checkpoint=tmp_path/'checkpoint.md'
    source='complete\r\n'+'x'*65536+'TAIL\n'
    checkpoint.write_bytes(source.encode())
    monkeypatch.setattr(tui,'LAST_RECOVERY_DRAFT',checkpoint)
    monkeypatch.setattr(tui,'compose_document',lambda *args,**kwargs:source)
    monkeypatch.setattr(tui.tempfile,'mkstemp',lambda *args,**kwargs: (_ for _ in ()).throw(OSError('disk full')))
    class Window:
        def clear(self): pass
        def refresh(self): pass
    state=tui._ConsoleState('mission',lambda *_:pytest.fail('must not dispatch failed save'),lambda *_:{},full=True)
    tui._compose_task_prompt(Window(),state)
    assert checkpoint.read_bytes()==source.encode()
    assert state.edit_initial_text==source
    assert 'disk full' in state.output[-1] and str(checkpoint) in state.output[-1]
